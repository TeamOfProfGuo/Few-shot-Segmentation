#!/bin/sh
PARTITION=Segmentation

dataset=$1
exp_name=$2
exp_dir=exp/${dataset}/${exp_name}
model_dir=${exp_dir}/model
result_dir=${exp_dir}/result
config=config/${dataset}/${dataset}_${exp_name}.yaml

mkdir -p ${model_dir} ${result_dir}
now=$(date +"%Y%m%d_%H%M%S")
cp test.sh test.py ${config} ${exp_dir}



echo "start"
singularity exec --nv \
            --overlay /scratch/lg154/python36/python36.ext3:ro \
            --overlay /scratch/lg154/sseg/dataset/coco2014.sqf:ro \
            /scratch/work/public/singularity/cuda11.2.2-cudnn8-devel-ubuntu20.04.sif \
            /bin/bash -c "source /ext3/env.sh; python test.py --config=${config} > ${result_dir}/test-$now.log 2>&1"
echo "finish"


# python3 -u test.py --config=${config} 2>&1 | tee ${result_dir}/test-$now.log