import argparse
import ast
import os
import sys
from clip import Clip
import random

import ffmpeg

sys.setrecursionlimit(10**6)

def _join_videos(listpath, outputpath):
    (
        ffmpeg
        .input(listpath, format='concat', safe=0)
        .output(outputpath, c='copy')
        .overwrite_output()
        .run()
    )


def single_inference(args):
    text = 'salt'
    if args.chill:
        text = 'chill'
        assert not args.pog
    if args.pog:
        text = 'pog'
        assert not args.chill
    if args.bbox is not None:
        clip = Clip(args.single_inference, bbox=ast.literal_eval(args.bbox), text=text)
    else:
        clip = Clip(args.single_inference, text=text)
    clip.inference_frameskip = args.frameskip
    clip.inference(args.model_path, audio_cutoff=args.audio_cutoff, arch=args.arch, batch_size=args.batch_size, use_sound=not args.no_sound, concat_full=args.concat_full, fp16=args.fp16)
    if args.benchmark:
        return
    clip.generate_annotated(args.name)


def highlights(args):
    paths = list()
    idx = 0
    while True:
        path = f'{args.prefix}{idx}.mp4'
        if os.path.exists(f'{args.prefix}{idx}.mp4'):
            paths.append(path)
            idx += 1
        else:
            break

    paths = sorted(paths)

    if not len(paths):
        assert os.path.exists(args.prefix)

    text = 'salt'
    if args.chill:
        text = 'chill'

    if len(paths) >= 1:
        print("joining videos...")
        tempvideolist = 'tempvideolist' + str(random.randint(0,2**32))
        basename = os.path.splitext(args.name)[0]
        tempconcatvideo = f'temp{basename}.mp4'
        with open(tempvideolist, 'w') as f:
            for path in paths:
                f.write(f'file \'{path}\'\n')
        _join_videos(tempvideolist, tempconcatvideo)
        if args.bbox is not None:
            clip = Clip(tempconcatvideo, bbox=ast.literal_eval(args.bbox), text=text)
        else:
            clip = Clip(tempconcatvideo, text=text)
        clip.inference_frameskip = args.frameskip
        clip.inference(args.model_path, audio_cutoff=args.audio_cutoff, arch=args.arch, batch_size=args.batch_size, use_sound=not args.no_sound, concat_full=args.concat_full, fp16=args.fp16)
        if args.benchmark:
            return
        clip.bin(args.bin_size)
        print(clip.bins)
        #clip.generate_highlights(bin_size=args.bin_size, output_path=args.name, percentile=args.percentile, threshold=args.threshold, delete_temp=args.delete_temp, adjacent=not args.no_adacjent)
        clip.generate_highlights_flex(bin_size=args.bin_size, output_path=args.name, threshold=args.threshold, delete_temp=args.delete_temp, notext=args.notext)
        os.unlink(tempconcatvideo)
        os.unlink(tempvideolist)
    else:
        path = args.prefix
        if args.bbox is not None:
            clip = Clip(path, bbox=ast.literal_eval(args.bbox), text=text)
        else:
            clip = Clip(path, text=text)
        clip.inference_frameskip = args.frameskip
        clip.inference(args.model_path, audio_cutoff=args.audio_cutoff, arch=args.arch, batch_size=args.batch_size, use_sound=not args.no_sound, concat_full=args.concat_full, fp16=args.fp16)
        if args.benchmark:
            return
        clip.bin(args.bin_size)
        print(clip.bins)
        #clip.generate_highlights(bin_size=args.bin_size, output_path=args.name, percentile=args.percentile, threshold=args.threshold, delete_temp=args.delete_temp, adjacent=not args.no_adjacent)
        clip.generate_highlights_flex(bin_size=args.bin_size, output_path=args.name, threshold=args.threshold, delete_temp=args.delete_temp, notext=args.notext)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--single-inference", help="file name for single full inference")
    parser.add_argument("-p", "--prefix", help="prefix of file name to parse")
    parser.add_argument("-n", "--name", help="name of output", required=True)
    parser.add_argument("-d", "--delete-temp", help="delete temporary clips", action='store_true')
    parser.add_argument("-b", "--benchmark", help="benchmark mode", action='store_true')
    parser.add_argument("--model-path", help="path to model checkpoint", default='model_best.pth.tar')
    parser.add_argument("-a", "--arch", help="model architecture to use", default='resnet18')
    parser.add_argument("--no-sound", help="no sound", action='store_true')
    parser.add_argument("--no-adjacent", help="don't append adjacent segments for highlights", action='store_true')
    parser.add_argument("--concat-full", help="concat full frame", action='store_true')
    parser.add_argument("--audio-cutoff", help="audio frequency cutoff", default=8000, type=int)
    parser.add_argument("--percentile", default=0.990, type=float)
    parser.add_argument("--threshold", default=0.7, type=float)
    parser.add_argument("--bin-size", default=5, type=int)
    parser.add_argument("--batch-size", default=32, type=int)
    parser.add_argument("--bbox", type=str)
    parser.add_argument("--frameskip", default=10, type=int)
    parser.add_argument("--chill", action='store_true')
    parser.add_argument("--pog", action='store_true')
    parser.add_argument("--gypsy", action='store_true')
    parser.add_argument("--artosis", action='store_true')
    parser.add_argument("--notext", action='store_true')
    parser.add_argument("--fp16", action='store_true')
    args = parser.parse_args()

    # shortcut some defaults for strimmers
    if args.gypsy:
        assert not args.artosis
        assert not args.pog
        args.bbox = "[0.77109375, 0.6875, 0.98828125, 1.0]"
        args.bin_size = 8
        args.threshold = 0.7
        args.chill = True
    if args.artosis:
        assert not args.gypsy
        assert not args.pog
        args.bbox = "[0.7833, 0.1296, 0.9682, 0.3694]"
        args.bin_size = 16
        args.threshold = 0.8
    if args.pog:
        assert not args.gypsy
        assert not args.artosis
        args.bbox = "[0.0, 0.0, 1.0, 1.0]"
        args.bin_size = 5
        args.threshold = 0.5

    assert args.single_inference is not None or args.prefix is not None
    if args.single_inference is not None:
        single_inference(args)
    else:
        highlights(args) 


if __name__ == '__main__':
    main()
