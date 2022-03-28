# Few-shot Segmentation Codebase

Adopted from [**PFENet**](https://github.com/dvlab-research/PFENet).

## Requisites
- Test Env: Python 3.9.7 (Singularity)
- Packages:
    - torch (1.10.2+cu113), torchvision (0.11.3+cu113)
    - numpy, scipy, pandas, tensorboardX
    - cv2

## Clone codebase
```
cd /scratch/$USER
git clone https://github.com/TeamOfProfGuo/Few-shot-Segmentation -b hmd-base
cd Few-shot-Segmentation
```

## Prepare Pascal-5i dataset
**Note:** Make sure the path in prepare_dataset.sh works for you.
```
cd /scratch/$USER/Few-shot-Segmentation
bash prepare_dataset.sh
```

## Prepare pretrained models
Download via <a href="https://drive.google.com/file/d/1rMPedZBKFXiWwRX3OHttvKuD1h9QRDbU/view?usp=sharing" target="_blank">this link</a>, and transfer the zip file to your project root on Greene.
```
cd /scratch/$USER/Few-shot-Segmentation
unzip initmodel.zip
```

## Prepare config
Modify the **data_root** under *config/pascal/pascal_split0_resnet50.yaml*.

## Training
**Note:** Modify the path in slurm scripts (as needed) before you start.
```
# switch to project root
cd /scratch/$USER/Few-shot-Segmentation

# train & save & test
sbatch train.slurm pascal split0_resnet50

# After the job starts:
cd exp/pascal/split0_resnet50/result && ls
head train-[some_time_info].log
# [2022-03-28 13:20:18,453 INFO train.py line 109 4100262] => creating model ...
# [2022-03-28 13:20:18,454 INFO train.py line 110 4100262] Classes: 2
# [2022-03-28 13:20:18,454 INFO train.py line 111 4100262] PFENet(
# [...]

# After the job ends:
[To be updated]
```

## Testing
[To be updated]