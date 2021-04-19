import argparse
import os


def get_parser():
    parser = argparse.ArgumentParser(description="merge CTC alignment to \
        format.data")
    parser.add_argument("align", type=str, help="CTC alignment file")
    parser.add_argument("data", type=str, help="Original format.data file")
    parser.add_argument("output", type=str, help="Output file")
    return parser


if __name__ == "__main__":
    parser = get_parser()
    args = parser.parse_args()
    if os.path.exists(args.align) and os.path.exists(args.data):
        pass
    else:
        print("Error: input files don't exist!")

    align_info = {}
    with open(args.align, 'r', encoding='utf8') as fp:
        for line in fp:
            seg = line.strip().split('\t')
            if len(seg) != 2:
                continue
            align_info[seg[0]] = seg[1]

    with open(args.output, 'w', encoding='utf8') as wfp:
        with open(args.data, 'r', encoding='utf') as fp:
            for line in fp:
                seg = line.strip().split('\t')
                if len(seg) != 7:
                    continue
                utt = seg[0].split(':')[1]
                if utt in align_info.keys():
                    ctc_idx_str = align_info[utt]
                    out_str = line.strip() + '\t' + "token_time:" + ctc_idx_str + '\n'
                    wfp.write(out_str)
                else:
                    print("Warning: utt {} doesn't has CTC alignment".format(utt))
