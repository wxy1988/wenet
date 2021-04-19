# Copyright (c) 2020 Mobvoi Inc. (authors: Binbin Zhang, Chao Yang)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import codecs
import copy
import logging
import random

import numpy as np
import torch
import torchaudio
import torchaudio.compliance.kaldi as kaldi
import yaml
from PIL import Image
from PIL.Image import BICUBIC
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset, DataLoader

import wenet.dataset.kaldi_io as kaldi_io
from wenet.dataset.wav_distortion import distort_wav_conf
from wenet.utils.common import IGNORE_ID

def _spec_augmentation(x,
                       warp_for_time=False,
                       num_t_mask=2,
                       num_f_mask=2,
                       max_t=50,
                       max_f=10,
                       max_w=80):
    """ Deep copy x and do spec augmentation then return it

    Args:
        x: input feature, T * F 2D
        num_t_mask: number of time mask to apply
        num_f_mask: number of freq mask to apply
        max_t: max width of time mask
        max_f: max width of freq mask
        max_w: max width of time warp

    Returns:
        augmented feature
    """
    y = np.copy(x)
    max_frames = y.shape[0]
    max_freq = y.shape[1]

    # time warp
    if warp_for_time and max_frames > max_w * 2:
        center = random.randrange(max_w, max_frames - max_w)
        warped = random.randrange(center - max_w, center + max_w) + 1

        left = Image.fromarray(x[:center]).resize((max_freq, warped), BICUBIC)
        right = Image.fromarray(x[center:]).resize((max_freq,
                                                   max_frames - warped),
                                                   BICUBIC)
        y = np.concatenate((left, right), 0)
    # time mask
    for i in range(num_t_mask):
        start = random.randint(0, max_frames - 1)
        length = random.randint(1, max_t)
        end = min(max_frames, start + length)
        y[start:end, :] = 0
    # freq mask
    for i in range(num_f_mask):
        start = random.randint(0, max_freq - 1)
        length = random.randint(1, max_f)
        end = min(max_freq, start + length)
        y[:, start:end] = 0
    return y

def _spec_substitute(x, max_t=20, num_t_sub=3):
    """ Deep copy x and do spec substitute then return it

    Args:
        x: input feature, T * F 2D
        max_t: max width of time substitute
        num_t_sub: number of time substitute to apply

    Returns:
        augmented feature
    """
    y = np.copy(x)
    max_frames = y.shape[0]
    for i in range(num_t_sub):
        start = random.randint(0, max_frames - 1)
        length = random.randint(1, max_t)
        end = min(max_frames, start + length)
        # only substitute the earlier time chosen randomly for current time
        pos = random.randint(0, start)
        y[start:end, :] = y[start - pos:end - pos, :]
    return y

def _waveform_distortion(waveform, distortion_methods_conf):
    """ Apply distortion on waveform

    This distortion will not change the length of the waveform.

    Args:
        waveform: numpy float tensor, (length,)
        distortion_methods_conf: a list of config for ditortion method.
            a method will be randomly selected by 'method_rate' and
            apply on the waveform.

    Returns:
        distorted waveform.
    """
    r = random.uniform(0, 1)
    acc = 0.0
    for distortion_method in distortion_methods_conf:
        method_rate = distortion_method['method_rate']
        acc += method_rate
        if r < acc:
            distortion_type = distortion_method['name']
            distortion_conf = distortion_method['params']
            point_rate = distortion_method['point_rate']
            return distort_wav_conf(waveform, distortion_type,
                                    distortion_conf , point_rate)
    return waveform

# add speed perturb when loading wav
# return augmented, sr
def _load_wav_with_speed(wav_file, speed):
    """ Load the wave from file and apply speed perpturbation

    Args:
        wav_file: input feature, T * F 2D

    Returns:
        augmented feature
    """
    if speed == 1.0:
        return torchaudio.load_wav(wav_file)
    else:
        si, _ = torchaudio.info(wav_file)
        E = torchaudio.sox_effects.SoxEffectsChain()
        E.append_effect_to_chain('speed', speed)
        E.append_effect_to_chain("rate", si.rate)
        E.set_input_file(wav_file)
        wav, sr = E.sox_build_flow_effects()
        # sox will normalize the waveform, scale to [-32768, 32767]
        wav = wav * (1 << 15)
        return wav, sr

def _extract_feature(batch, speed_perturb, wav_distortion_conf,
                     feature_extraction_conf, use_ctc_ref=False):
    """ Extract acoustic fbank feature from origin waveform.

    Speed perturbation and wave amplitude distortion is optional.

    Args:
        batch: a list of tuple (wav id , wave path).
        speed_perturb: bool, whether or not to use speed pertubation.
        wav_distortion_conf: a dict , the config of wave amplitude distortion.
        feature_extraction_conf:a dict , the config of fbank extraction.

    Returns:
        (keys, feats, labels)
    """
    keys = []
    feats = []
    lengths = []
    wav_dither = wav_distortion_conf['wav_dither']
    wav_distortion_rate = wav_distortion_conf['wav_distortion_rate']
    distortion_methods_conf = wav_distortion_conf['distortion_methods']
    if speed_perturb:
        speeds = [1.0, 1.1, 0.9]
        weights = [1, 1, 1]
        speed = random.choices(speeds, weights, k=1)[0]
        # speed = random.choice(speeds)
    for i, x in enumerate(batch):
        try:
            if speed_perturb:
                waveform, sample_rate = _load_wav_with_speed(x[1], speed)
            else:
                waveform, sample_rate = torchaudio.load_wav(x[1])
            if wav_distortion_rate > 0.0:
                r = random.uniform(0, 1)
                if r < wav_distortion_rate:
                    waveform = waveform.detach().numpy()
                    waveform = _waveform_distortion(waveform,
                                                    distortion_methods_conf)
                    waveform = torch.from_numpy(waveform)
            mat = kaldi.fbank(
                waveform,
                num_mel_bins=feature_extraction_conf['mel_bins'],
                frame_length=feature_extraction_conf['frame_length'],
                frame_shift=feature_extraction_conf['frame_shift'],
                dither=wav_dither,
                energy_floor=0.0,
                sample_frequency=sample_rate
            )
            mat = mat.detach().numpy()
            feats.append(mat)
            keys.append(x[0])
            lengths.append(mat.shape[0])
        except (Exception) as e:
            print(e)
            logging.warn('read utterance {} error'.format(x[0]))
            pass
    # Sort it because sorting is required in pack/pad operation
    order = np.argsort(lengths)[::-1]
    sorted_keys = [keys[i] for i in order]
    sorted_feats = [feats[i] for i in order]
    labels = [x[2].split() for x in batch]
    labels = [np.fromiter(map(int, x), dtype=np.int32) for x in labels]
    sorted_labels = [labels[i] for i in order]
    sorted_ctc_time_idx = None
    if use_ctc_ref:
        ctc_time_idx = [x[3].split() for x in batch]
        if speed_perturb:
            # TODO: maybe need check the boundary whether the last index exceeds.
            ctc_time_idx_np = [np.ceil(np.fromiter(
                map(int, x), dtype=np.int32) / speed).astype(np.int32) \
                               for x in ctc_time_idx]
        else:
            ctc_time_idx_np = [np.fromiter(
                map(int, x), dtype=np.int32) for x in ctc_time_idx]
        sorted_ctc_time_idx = [ctc_time_idx_np[i] for i in order]
    return sorted_keys, sorted_feats, sorted_labels, sorted_ctc_time_idx

def _load_feature(batch, use_ctc_ref=False):
    """ Load acoustic feature from files.

    The features have been prepared in previous step, usualy by Kaldi.

    Args:
        batch: a list of tuple (wav id , feature ark path).

    Returns:
        (keys, feats, labels)
    """
    keys = []
    feats = []
    lengths = []
    for i, x in enumerate(batch):
        try:
            mat = kaldi_io.read_mat(x[1])
            feats.append(mat)
            keys.append(x[0])
            lengths.append(mat.shape[0])
        except (Exception):
            # logging.warn('read utterance {} error'.format(x[0]))
            pass
    # Sort it because sorting is required in pack/pad operation
    order = np.argsort(lengths)[::-1]
    sorted_keys = [keys[i] for i in order]
    sorted_feats = [feats[i] for i in order]
    labels = [x[2].split() for x in batch]
    labels = [np.fromiter(map(int, x), dtype=np.int32) for x in labels]
    sorted_labels = [labels[i] for i in order]
    sorted_ctc_time_idx = None
    if use_ctc_ref:
        ctc_time_idx = [x[3].split() for x in batch]
        sorted_ctc_time_idx = [np.fromiter(
            map(int, x), dtype=np.int32) for x in ctc_time_idx]
    return sorted_keys, sorted_feats, sorted_labels, sorted_ctc_time_idx

class CollateFunc(object):
    """ Collate function for AudioDataset
    """
    def __init__(self,
                 feature_dither=0.0,
                 speed_perturb=False,
                 spec_aug=False,
                 spec_aug_conf=None,
                 spec_sub=False,
                 spec_sub_conf=None,
                 raw_wav=True,
                 feature_extraction_conf=None,
                 wav_distortion_conf=None,
                 use_ctc_ref=False,
                 ):
        """
        Args:
            raw_wav:
                    True if input is raw wav and feature extraction is needed.
                    False if input is extracted feature
        """
        self.wav_distortion_conf = wav_distortion_conf
        self.feature_extraction_conf = feature_extraction_conf
        self.spec_aug = spec_aug
        self.feature_dither = feature_dither
        self.speed_perturb = speed_perturb
        self.raw_wav = raw_wav
        self.spec_aug_conf = spec_aug_conf
        self.spec_sub = spec_sub
        self.spec_sub_conf = spec_sub_conf
        self.use_ctc_ref = use_ctc_ref
        print("coll use_ctc_ref:{}".format(use_ctc_ref))

    def __call__(self, batch):
        assert (len(batch) == 1)
        if self.raw_wav:
            keys, xs, ys, ctc_time_idx = _extract_feature(
                batch[0],
                self.speed_perturb,
                self.wav_distortion_conf,
                self.feature_extraction_conf,
                self.use_ctc_ref)

        else:
            keys, xs, ys, ctc_time_idx = _load_feature(batch[0], self.use_ctc_ref)

        train_flag = True
        if ys is None:
            train_flag = False

        # optional feature dither d ~ (-a, a) on fbank feature
        # a ~ (0, 0.5)
        if self.feature_dither != 0.0:
            a = random.uniform(0, self.feature_dither)
            xs = [x + (np.random.random_sample(x.shape) - 0.5) * a for x in xs]

        # optinoal spec substitute
        if self.spec_sub:
            xs = [_spec_substitute(x, **self.spec_sub_conf) for x in xs]

        # optinoal spec augmentation
        if self.spec_aug:
            xs = [_spec_augmentation(x, **self.spec_aug_conf) for x in xs]

        # padding
        xs_lengths = torch.from_numpy(
            np.array([x.shape[0] for x in xs], dtype=np.int32))

        # pad_sequence will FAIL in case xs is empty
        if len(xs) > 0:
            xs_pad = pad_sequence([torch.from_numpy(x).float() for x in xs],
                                  True, 0)
        else:
            xs_pad = torch.Tensor(xs)        

        ys_time_idx = None
        if train_flag:
            ys_lengths = torch.from_numpy(
                np.array([y.shape[0] for y in ys], dtype=np.int32))
            if len(ys) > 0:
                ys_pad = pad_sequence([torch.from_numpy(y).int() for y in ys],
                                      True, IGNORE_ID)
                if self.use_ctc_ref:
                    ys_time_idx = pad_sequence([
                        torch.from_numpy(y).long() for y in ctc_time_idx], True, IGNORE_ID)
            else:
                ys_pad = torch.Tensor(ys)
                if self.use_ctc_ref:
                    ys_time_idx = torch.Tensor(ctc_time_idx).long()
        else:
            ys_pad = None
            ys_lengths = None
        return keys, xs_pad, ys_pad, xs_lengths, ys_lengths, ys_time_idx


class AudioDataset(Dataset):
    def __init__(self,
                 data_file,
                 max_length=10240,
                 min_length=0,
                 batch_type='static',
                 batch_size=1,
                 max_frames_in_batch=0,
                 sort=True,
                 raw_wav=True,
                 use_ctc_ref=False):
        """Dataset for loading audio data.

        Attributes::
            data_file: input data file
                Plain text data file, each line contains following 7 fields,
                which is split by '\t':
                    utt:utt1
                    feat:tmp/data/file1.wav or feat:tmp/data/fbank.ark:30
                    feat_shape: 4.95(in seconds) or feat_shape:495,80(495 is in frames)
                    text:i love you
                    token: i <space> l o v e <space> y o u
                    tokenid: int id of this token
                    token_shape: M,N    # M is the number of token, N is vocab size
                    token_time: t1 t2 t3 ... # the number is equal to token numbers
            max_length: drop utterance which is greater than max_length(10ms)
            min_length: drop utterance which is less than min_length(10ms)
            batch_type: static or dynamic, see max_frames_in_batch(dynamic)
            batch_size: number of utterances in a batch,
               it's for static batch size.
            max_frames_in_batch: max feature frames in a batch,
               when batch_type is dynamic, it's for dynamic batch size.
               Then batch_size is ignored, we will keep filling the
               batch until the total frames in batch up to max_frames_in_batch.
            sort: whether to sort all data, so the utterance with the same
               length could be filled in a same batch.
            raw_wav: use raw wave or extracted featute.
                if raw wave is used, dynamic waveform-level augmentation could be used
                and the feature is extracted by torchaudio.
                if extracted featute(e.g. by kaldi) is used, only feature-level
                augmentation such as specaug could be used.
        """
        assert batch_type in ['static', 'dynamic']
        data = []
        print("use_ctc_ref: {}".format(use_ctc_ref))
        # Open in utf8 mode since meet encoding problem
        with codecs.open(data_file, 'r', encoding='utf-8') as f:
            for line in f:
                arr = line.strip().split('\t')
                if len(arr) != 7 and not use_ctc_ref:
                    continue
                key = arr[0].split(':')[1]
                tokenid = arr[5].split(':')[1]
                output_dim = int(arr[6].split(':')[1].split(',')[1])
                if raw_wav:
                    wav_path = ':'.join(arr[1].split(':')[1:])
                    duration = int(float(arr[2].split(':')[1]) * 1000 / 10)
                    if use_ctc_ref:
                        ctc_ref_time = arr[7].split(':')[1]
                        data.append((key, wav_path, duration, tokenid, ctc_ref_time))
                    else:
                        data.append((key, wav_path, duration, tokenid))
                else:
                    feat_ark = ':'.join(arr[1].split(':')[1:])
                    feat_info = arr[2].split(':')[1].split(',')
                    feat_dim = int(feat_info[1].strip())
                    num_frames = int(feat_info[0].strip())
                    if use_ctc_ref:
                        ctc_ref_time = arr[7].split(':')[1]
                        data.append((key, feat_ark, num_frames, tokenid, ctc_ref_time))
                    else:
                        data.append((key, feat_ark, num_frames, tokenid))
                    self.input_dim = feat_dim
                self.output_dim = output_dim
        if sort:
            data = sorted(data, key=lambda x: x[2])
        valid_data = []
        for i in range(len(data)):
            length = data[i][2]
            if length > max_length or length < min_length:
                # logging.warn('ignore utterance {} feature {}'.format(
                #     data[i][0], length))
                pass
            else:
                valid_data.append(data[i])
        data = valid_data
        self.minibatch = []
        num_data = len(data)
        # Dynamic batch size
        if batch_type == 'dynamic':
            assert (max_frames_in_batch > 0)
            self.minibatch.append([])
            num_frames_in_batch = 0
            for i in range(num_data):
                length = data[i][2]
                num_frames_in_batch += length
                if num_frames_in_batch > max_frames_in_batch:
                    self.minibatch.append([])
                    num_frames_in_batch = length
                if use_ctc_ref:
                    self.minibatch[-1].append((
                        data[i][0], data[i][1], data[i][3], data[i][4]))
                else:
                    self.minibatch[-1].append((data[i][0], data[i][1], data[i][3]))
        # Static batch size
        else:
            cur = 0
            while cur < num_data:
                end = min(cur + batch_size, num_data)
                item = []
                for i in range(cur, end):
                    if use_ctc_ref:
                        item.append((data[i][0], data[i][1], data[i][3], data[i][4]))
                    else:
                        item.append((data[i][0], data[i][1], data[i][3]))
                self.minibatch.append(item)
                cur = end

    def __len__(self):
        return len(self.minibatch)

    def __getitem__(self, idx):
        return self.minibatch[idx]
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('type', help='config file')
    parser.add_argument('config_file', help='config file')
    parser.add_argument('data_file', help='input data file')
    args = parser.parse_args()

    with open(args.config_file, 'r') as fin:
        configs = yaml.load(fin)

    # Init dataset and data loader
    collate_conf = copy.copy(configs['collate_conf'])
    if args.type == 'raw_wav':
        raw_wav = True
    else:
        raw_wav = False
    collate_func = CollateFunc(**collate_conf, raw_wav=raw_wav)
    dataset_conf = configs.get('dataset_conf', {})
    dataset = AudioDataset(args.data_file, **dataset_conf, raw_wav=raw_wav)

    data_loader = DataLoader(dataset,
                             batch_size=1,
                             shuffle=True,
                             sampler=None,
                             num_workers=0,
                             collate_fn=collate_func)

    for i, batch in enumerate(data_loader):
        print(i)
        # print(batch[1].shape)
