nj=16
feat_dir=raw_wav
dict=data/dict/lang_char.txt

train_set=train
# Optional train_config
# 1. conf/train_transformer.yaml: Standard transformer
# 2. conf/train_conformer.yaml: Standard conformer
# 3. conf/train_unified_conformer.yaml: Unified dynamic chunk causal conformer
# 4. conf/train_unified_transformer.yaml: Unified dynamic chunk transformer
train_config=conf/train_conformer.yaml
cmvn=true
dir=exp/conformer
align_dir=$dir/align_data

tools/ctc_align.sh $feat_dir/$train_set $dir/final.pt $align_dir