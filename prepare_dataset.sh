#!/bin/bash

# modify the path if needed
dataset_dir=/scratch/$USER/dataset
codebase_dir=/scratch/$USER/Few-shot-Segmentation
if [ ! -d $dataset_dir ]; then
    mkdir $dataset_dir
fi

# prepare [PASCAL VOC 2012] dataset
cd $dataset_dir
wget http://host.robots.ox.ac.uk/pascal/VOC/voc2012/VOCtrainval_11-May-2012.tar
tar -xvf VOCtrainval_11-May-2012.tar

# prepare [SBD] dataset
cd ./VOCdevkit/VOC2012
wget https://github.com/TeamOfProfGuo/Codebase-Files/raw/main/SegmentationClassAug.zip
unzip SegmentationClassAug.zip
