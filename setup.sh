conda create -n e2fgvi python=3.8

conda activate e2fgvi

pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

pip install -U openmim
mim install mmcv
