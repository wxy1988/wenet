#! /bin/bash
# Do the alignment with CTC to get the time step

echo "$0 $*" >&2 # Print the command line for logging
. ./path.sh

nj=1
cmd=run.pl
batch=0

. tools/parse_options.sh
if [ $# -ne 3 ]; then
    echo "Usage: $0 <data-dir> <model> <align-dir>"
    exit 1;
fi

datadir=$1
model=$2
output=$3

modeldir=$(dirname $model)

[ -d $output ] && echo "Directory $output exists, please delete it" && exit 1;

mkdir -p $output/log

for f in $datadir/format.data $model $modeldir/train.yaml; do
    if [ ! -f $f ]; then
        echo "Error: $f doesn't exist!"
        exit 1;
    fi
done

opts=
if [ $batch -ne 0 ]; then
    opts+="--batch_size $batch"
fi

tmpdir=$(mktemp -d $datadir/tmp-XXXXX)
split -d -n l/$nj --additional-suffix=.slice $datadir/format.data $tmpdir/align.

for slice in $(ls $tmpdir/align.*.slice); do
{
    data_name=$(basename -s .slice $slice)
    python wenet/bin/ctc_align.py ${opts} --config $modeldir/train.yaml --align_data $slice \
        --checkpoint $model --result_file $output/${data_name} >$output/log/ctc_align.${data_name}.log 2>&1
} &
done
wait
cat $output/align.* >$output/align.all
echo "All alignment tasks done."