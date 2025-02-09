# -*- coding: utf-8 -*-
import cv2
from PIL import Image
import numpy as np
import importlib
import os
import argparse
from tqdm import tqdm
import matplotlib.pyplot as plt
from matplotlib import animation
import torch
from itertools import islice

from core.utils import to_tensors


# sample reference frames from the whole video
def get_ref_index(f, neighbor_ids, length, ref_length, num_ref):
    ref_index = []
    if num_ref == -1:
        for i in range(0, length, ref_length):
            if i not in neighbor_ids:
                ref_index.append(i)
    else:
        start_idx = max(0, f - ref_length * (num_ref // 2))
        end_idx = min(length, f + ref_length * (num_ref // 2))
        for i in range(start_idx, end_idx, ref_length):
            if i not in neighbor_ids:
                if len(ref_index) > num_ref:
                    break
                ref_index.append(i)
    return ref_index


# read frame-wise masks
def read_mask(mpath, size):
    masks = []
    mnames = os.listdir(mpath)
    mnames.sort()
    for mp in mnames:
        m = Image.open(os.path.join(mpath, mp))
        m = m.resize(size, Image.NEAREST)
        m = np.array(m.convert("L"))
        m = np.array(m > 0).astype(np.uint8)
        m = cv2.dilate(
            m, cv2.getStructuringElement(cv2.MORPH_CROSS, (10, 10)), iterations=8
        )
        masks.append(Image.fromarray(m * 255))
    return masks


#  read frames from video
def read_frame_from_videos(args):
    vname = args.video
    frames = []
    if args.use_mp4:
        vidcap = cv2.VideoCapture(vname)
        success, image = vidcap.read()
        count = 0
        while success:
            image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            frames.append(image)
            success, image = vidcap.read()
            count += 1
    else:
        lst = os.listdir(vname)
        lst.sort()
        fr_lst = [vname + "/" + name for name in lst]
        for fr in fr_lst:
            image = cv2.imread(fr)
            image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            frames.append(image)
    return frames


# resize frames
def resize_frames(frames, size=None):
    if size is not None:
        frames = [f.resize(size) for f in frames]
    else:
        size = frames[0].size
    return frames, size


def setup_inpainter_model(model_name, ckpt):
    # set up models

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    net = importlib.import_module("model." + model_name)
    model = net.InpaintGenerator().to(device)
    data = torch.load(ckpt, map_location=device)
    model.load_state_dict(data)
    model.eval()

    return model, device


def main_worker(
    video,
    ckpt,
    mask,
    out_file,
    model_name,
    step=10,
    num_ref=-1,
    neighbor_stride=5,
    savefps=24,
    set_size=False,
    width=None,
    height=None,
    model=None,
    device=None,
):
    args = argparse.Namespace()
    args.video = video
    args.ckpt = ckpt
    args.mask = mask
    args.out_file = out_file
    args.model = model_name
    args.step = step
    args.num_ref = num_ref
    args.neighbor_stride = neighbor_stride
    args.savefps = savefps
    args.set_size = set_size
    args.width = width
    args.height = height

    if model is None:
        model, device = setup_inpainter_model(args)

    if args.model == "e2fgvi":
        size = (432, 240)
    elif args.set_size:
        size = (args.width, args.height)
    else:
        size = None

    # prepare datset
    args.use_mp4 = True if args.video.endswith(".mp4") else False
    frames = read_frame_from_videos(args)
    frames, size = resize_frames(frames, size)
    h, w = size[1], size[0]
    video_length = len(frames)
    imgs = to_tensors()(frames).unsqueeze(0) * 2 - 1
    frames = [np.array(f).astype(np.uint8) for f in frames]

    masks = read_mask(args.mask, size)
    binary_masks = [
        np.expand_dims((np.array(m) != 0).astype(np.uint8), 2) for m in masks
    ]
    masks = to_tensors()(masks).unsqueeze(0)
    imgs, masks = imgs.to(device), masks.to(device)
    comp_frames = [None] * video_length

    # completing holes by e2fgvi
    for f in tqdm(range(0, video_length, neighbor_stride)):
        neighbor_ids = [
            i
            for i in range(
                max(0, f - neighbor_stride), min(video_length, f + neighbor_stride + 1)
            )
        ]
        ref_ids = get_ref_index(f, neighbor_ids, video_length, args.step, args.num_ref)
        selected_imgs = imgs[:1, neighbor_ids + ref_ids, :, :, :]
        selected_masks = masks[:1, neighbor_ids + ref_ids, :, :, :]
        with torch.no_grad():
            masked_imgs = selected_imgs * (1 - selected_masks)
            mod_size_h = 60
            mod_size_w = 108
            h_pad = (mod_size_h - h % mod_size_h) % mod_size_h
            w_pad = (mod_size_w - w % mod_size_w) % mod_size_w
            masked_imgs = torch.cat([masked_imgs, torch.flip(masked_imgs, [3])], 3)[
                :, :, :, : h + h_pad, :
            ]
            masked_imgs = torch.cat([masked_imgs, torch.flip(masked_imgs, [4])], 4)[
                :, :, :, :, : w + w_pad
            ]
            pred_imgs, _ = model(masked_imgs, len(neighbor_ids))
            pred_imgs = pred_imgs[:, :, :h, :w]
            pred_imgs = (pred_imgs + 1) / 2
            pred_imgs = pred_imgs.cpu().permute(0, 2, 3, 1).numpy() * 255
            torch.cuda.empty_cache()
            for i in range(len(neighbor_ids)):
                idx = neighbor_ids[i]
                img = np.array(pred_imgs[i]).astype(np.uint8) * binary_masks[
                    idx
                ] + frames[idx] * (1 - binary_masks[idx])
                if comp_frames[idx] is None:
                    comp_frames[idx] = img
                else:
                    comp_frames[idx] = (
                        comp_frames[idx].astype(np.float32) * 0.5
                        + img.astype(np.float32) * 0.5
                    )

    # saving videos
    writer = cv2.VideoWriter(
        args.out_file, cv2.VideoWriter_fourcc(*"mp4v"), savefps, (w, h)
    )
    for f in range(video_length):
        comp = comp_frames[f].astype(np.uint8)
        writer.write(cv2.cvtColor(comp, cv2.COLOR_BGR2RGB))
    writer.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2FGVI")
    parser.add_argument("-v", "--video", type=str, required=True)
    parser.add_argument("-c", "--ckpt", type=str, required=True)
    parser.add_argument("-m", "--mask", type=str, required=True)
    parser.add_argument("-o", "--out_file", type=str, required=True)
    parser.add_argument("--model", type=str, choices=["e2fgvi", "e2fgvi_hq"])
    parser.add_argument("--step", type=int, default=10)
    parser.add_argument("--num_ref", type=int, default=-1)
    parser.add_argument("--neighbor_stride", type=int, default=5)
    parser.add_argument("--savefps", type=int, default=24)

    # args for e2fgvi_hq (which can handle videos with arbitrary resolution)
    parser.add_argument("--set_size", action="store_true", default=False)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)

    args = parser.parse_args()

    ref_length = args.step  # ref_step
    num_ref = args.num_ref
    neighbor_stride = args.neighbor_stride
    default_fps = args.savefps

    main_worker()
