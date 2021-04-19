#! /bin/bash

stage=1
stop_stage=1
nj=20
feat_dir=raw_wav
dict=data/dict/lang_char.txt

train_set=train
dev_set=dev
# Optional train_config
# 1. conf/train_transformer.yaml: Standard transformer
# 2. conf/train_conformer.yaml: Standard conformer
# 3. conf/train_unified_conformer.yaml: Unified dynamic chunk causal conformer
# 4. conf/train_unified_transformer.yaml: Unified dynamic chunk transformer
train_config=conf/train_conformer.yaml
cmvn=true
dir=exp/conformer
align_dir=$dir/align_data
dev_align_dir=$dir/dev_align_data

if [ $stage -le 0 ] && [ $stop_stage -ge 0 ]; then
    tools/ctc_align.sh --nj $nj $feat_dir/$train_set $dir/final.pt $align_dir
fi

if [ $stage -le 1 ] && [ $stop_stage -ge 1 ]; then
    tools/ctc_align.sh --nj $nj $feat_dir/$dev_set $dir/final.pt $dev_align_dir
    python tools/merge_align2format.py $dev_align_dir/align.all $feat_dir/$dev_set/format.data $dir/dev_align_format.data
fi