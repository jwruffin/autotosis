import argparse
import ast
import csv
import os
import subprocess
import shutil
import numpy as np
import torch
import torchvision.transforms as transforms
from torch.utils.data import Dataset
from progress.bar import Bar
from PIL import Image
# 'ffmpeg-python', not 'ffmpeg' in pip
import ffmpeg

from artosisnet import get_inference_model, get_prediction

INFERENCE_FRAMESKIP = 30
DEFAULT_FACE_BBOX = [0.7635, 0.1056, 0.9802, 0.4009]


normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])


def frame_to_img(filename, output_resolution, crop=False, crop_bbox=None, blackout_dims=None, concat_full=False, sound_filename=None):
    im = Image.open(filename)
    if blackout_dims is not None:
        box = Image.new('RGB', (blackout_dims[2], blackout_dims[3]), 'black')
        im.paste(box, (blackout_dims[0], blackout_dims[1]))
    height = im.height
    width = im.width
    if crop:
        im2 = im.crop((int(crop_bbox[0]*width),
                       int(crop_bbox[1]*height),
                       int(crop_bbox[2]*width),
                       int(crop_bbox[3]*height)))
        im2 = im2.resize((output_resolution, output_resolution))
        im3 = im.resize((output_resolution, output_resolution))
        # include the full frame in the data by concating it to the crop
        if concat_full:
            new_img = Image.new('RGB', (output_resolution, 2*output_resolution))
            new_img.paste(im2)
            new_img.paste(im3, (0, output_resolution))
            im2 = new_img
    else:
        im2 = im.resize((output_resolution, output_resolution))
    if sound_filename is not None:
        new_img = Image.new("RGB", (output_resolution, im2.size[1] + output_resolution))
        new_img.paste(im2)
        sound_img = Image.open(sound_filename)
        sound_img = sound_img.resize((output_resolution, output_resolution))
        new_img.paste(sound_img, (0, im2.size[1]))
        im2 = new_img
    return im2


class InferenceFrames(Dataset):
    def __init__(self, jpg_filenames, crop, output_resolution, face_bbox, sound_filenames):
        self.jpg_filenames = jpg_filenames
        self.crop = crop
        self.output_resolution = output_resolution
        self.face_bbox = face_bbox
        self.use_sound = use_sound
        self.sound_filenames = sound_filenames

    def __len__(self):
        return len(self.jpg_filenames)

    def __getitem__(self, idx):
        filename = self.jpg_filenames[idx]
        sound_filename = None
        if sound_filenames is not None:
            sound_filename = self.sound_filenames[idx]
        im2 = frame_to_img(filename, self.output_resolution, self.crop, self.face_bbox, sound_filename=sound_filename)
        t = transforms.ToTensor()(im2)
        t = normalize(t)
        return t, idx


# TODO: avoid hardcoded 1920x1080 resolution
class Clip(object):
    def __init__(self, filename, positive_segments=None,
                 face_bbox=DEFAULT_FACE_BBOX,
                 inference_frameskip=INFERENCE_FRAMESKIP):
        self.filename = filename
        if not os.path.exists(self.filename):
            raise ValueError('clip source not found')
        if positive_segments is not None: 
            self.positive_segments = positive_segments
        else:
            self.positive_segments = list()
        self.face_bbox = face_bbox 
        probe = ffmpeg.probe(filename)
        #print(probe)

        for meta in probe['streams']:
            if meta['codec_type'] == 'video': 
                video_meta = meta
                break
        # get metadata for video clip
        self.height = int(video_meta['height'])
        self.width = int(video_meta['width'])
        self.box_width = 260
        self.box_height = 80
        # WOW, this looks unsafe
        self.framerate = eval(video_meta['avg_frame_rate'])
        self.inference_frameskip = inference_frameskip
        self.duration = float(video_meta['duration'])
        if 'nb_frames' in video_meta:
            self.nb_frames = int(video_meta['nb_frames'])
        else:
            self.nb_frames = int(float(video_meta['duration'])*self.framerate)

    def read_frame_as_jpg(self, frame_num):
        out, err = (
            ffmpeg
            .input(self.filename)
            .filter_('select', 'gte(n,{})'.format(frame_num))
            .output('pipe:', vframes=2, format='image2', vcodec='mjpeg')
            .global_args('-loglevel', 'quiet')
            .run(capture_stdout=True)
        )
        return out

    def generate_data2(self, dest_path, crop=True, output_resolution=256, concat_full=True, use_sound=True):
        # unload all of the frames even if it's extra work because crap is fast
        # TODO support frameskip? (or not because maybe more data is just better)
        pos_path = os.path.join(dest_path, '1')
        neg_path = os.path.join(dest_path, '0')
        sound_path = os.path.join(dest_path, 'sound')

        if not os.path.exists(pos_path):
            os.makedirs(pos_path)
        if not os.path.exists(neg_path):
            os.makedirs(neg_path)
        if not os.path.exists(sound_path):
            os.makedirs(sound_path)

        basename = os.path.splitext(os.path.basename(self.filename))[0]
        fps_str = f'fps={str(int(self.framerate))}'
        jpeg_str = os.path.join(dest_path, f'{basename}_%d.jpg')
        sound_jpeg_str = os.path.join(sound_path, f'{basename}_sound_%d.jpg')
        newbasename = basename + '_'
        ffmpeg_cmd = ['ffmpeg', '-i', self.filename, '-q:v', '1', '-vf', fps_str, jpeg_str]
        print(ffmpeg_cmd)
        subprocess.call(ffmpeg_cmd)
        temp_spectro_path = os.path.join(sound_path, f'{basename}_sound.mp4')
        ffmpeg_spectro_cmd = ['ffmpeg', '-i', self.filename, '-filter_complex', '[0:a]showspectrum=s=512x512:mode=combined:slide=scroll:saturation=0.2:scale=log:color=intensity:stop=8000,format=yuv420p[v]', '-map', '[v]', '-map', '0:a', '-b:v', '700k', '-b:a', '360k', temp_spectro_path]
        subprocess.call(ffmpeg_spectro_cmd)
        ffmpeg_sound_cmd = ['ffmpeg', '-i', temp_spectro_path, '-q:v', '1', '-vf', fps_str, sound_jpeg_str]
        subprocess.call(ffmpeg_sound_cmd)
        os.unlink(temp_spectro_path)
        for dirpath, dirnames, filenames in os.walk(dest_path):
            if dirpath == pos_path or dirpath == neg_path:
                continue
            bar = Bar('generating progress', max=len(filenames))
            for filename in filenames:
                name, ext = os.path.splitext(filename)
                if ext == '.jpg':
                    if '_sound_' in filename:
                        continue
                    frame_num = int(name.split(newbasename)[1]) - 1
                    time = frame_num/self.framerate
                    label = '0'
                    for interval in self.positive_segments:
                        if time >= interval[0] and time <= interval[1]:
                            label = '1'
                            break
                    dst = os.path.join(dest_path, label)
                    dst = os.path.join(dst, filename)
                    src = os.path.join(dirpath, filename) 
                    shutil.move(src, dst) 
                    im = Image.open(dst)
                    height = im.height
                    width = im.width
                    blackout_dims = [1920//2 - self.box_width//2, 900, self.box_width, self.box_height]
                    sound_dst = None
                    if use_sound:
                        offset = 1
                        sound_dst = os.path.join(sound_path, f'{basename}_sound_{frame_num + offset}.jpg')
                        while not os.path.exists(sound_dst):
                            offset -= 1
                            sound_dst = os.path.join(sound_path, f'{basename}_sound_{frame_num + offset}.jpg')
                            assert offset > -10, f'could not find {basename}_sound_{frame_num + 1}.jpg'
                    im2 = frame_to_img(dst, output_resolution, crop, self.face_bbox, blackout_dims, concat_full=concat_full, sound_filename=sound_dst)
                    im2.save(dst, quality=95)
                bar.next()
        bar.finish()
        shutil.rmtree(sound_path)
    #def _inference_jpg(self, inference_model, jpg_filenames, crop, output_resolution):
    #    ims = list()
    #    for filename in jpg_filenames:
    #    preds = get_prediction(ims, inference_model)
    #    jpg_inference_results = [float(preds[i,1]) for i in range(len(ims))]
    #    assert len(jpg_inference_results) == len(jpg_filenames)
    #    return jpg_inference_results

    def inference(self, model_path, arch='resnet18', crop=True, output_resolution=256, batch_size=64, concat_full=True, use_sound=True):
        tempdir = 'temp/'
        if not os.path.exists(tempdir):
            os.makedirs(tempdir)

        inference_model = get_inference_model(model_path, arch)
        basename = os.path.splitext(os.path.basename(self.filename))[0]
        rounded_framerate = int(np.round(self.framerate))
        assert rounded_framerate % self.inference_frameskip == 0
        res_str = f'{self.width}x{self.height}'
        inference_fps = int(np.round(self.framerate/self.inference_frameskip))
        fps_str = f'fps={inference_fps}'
        jpeg_str = os.path.join(tempdir, f'{basename}_%d.jpg')
        sound_jpeg_str = os.path.join(tempdir, f'{basename}_sound_%d.jpg')
        newbasename = basename + '_'
        ffmpeg_cmd = ['ffmpeg', '-i', self.filename, '-s', res_str, '-q:v', '10', '-vf', fps_str, jpeg_str]
        print(ffmpeg_cmd)
        subprocess.call(ffmpeg_cmd)
        ffmpeg_spectro_cmd = ['ffmpeg', '-i', self.filename, '-filter_complex', '[0:a]showspectrum=s=512x512:mode=combined:slide=scroll:saturation=0.2:scale=log:color=intensity:stop=8000,format=yuv420p[v]', '-map', '[v]', '-map', '0:a', '-b:v', '700k', '-b:a', '360k', f'{basename}_sound.mp4']
        subprocess.call(ffmpeg_spectro_cmd)
        ffmpeg_sound_cmd = ['ffmpeg', '-i', f'{basename}_sound.mp4', '-q:v', '1', '-vf', fps_str, sound_jpeg_str]
        subprocess.call(ffmpeg_sound_cmd)
        os.unlink(f'{basename}_sound.mp4')

        print("duration:", self.duration)
          
        inference_results = None
        inference_results = [list() for i in range(int(np.ceil(self.duration)))]
        sound_filenames = None
        
        jpg_filenames = list()
        time_idxs = list()
        true_frame_nums = list()
        if use_sound:
            sound_filenames = list()

        for dirpath, dirnames, filenames in os.walk(tempdir):
            print(len(filenames))
            #batch_count = 0
            for filename in filenames:
                if newbasename not in filename:
                    continue
                name, ext = os.path.splitext(filename)
                if ext == '.jpg':
                    if '_sound_' in filename:
                        continue
                    jpg_filenames.append(os.path.join(dirpath, filename))
                    frame_num = int(name.split(newbasename)[1]) - 1
                    if sound_filenames is not None:
                        sound_filename = os.path.join(dirpath, f'{basename}_sound_{frame_num + offset}.jpg')
                        while not os.path.exists(sound_filename):
                            offset -= 1
                            sound_filename = os.path.join(dirpath, f'{basename}_sound_{frame_num + offset}.jpg')
                            assert offset > -10, f'could not find {basename}_sound_{frame_num + 1}.jpg'
                        sound_filenames.append(sound_filename)
                        assert os.path.exists(sound_filenames[-1])
                    time = (frame_num)/inference_fps
                    true_frame_num = int((frame_num) * self.framerate/inference_fps)
                    time_idx = int(time)
                    time_idxs.append(time_idx)
                    true_frame_nums.append(true_frame_num)
        assert len(jpg_filenames) == len(time_idxs)
        assert len(true_frame_nums) == len(time_idxs)


        dataset = InferenceFrames(jpg_filenames, crop, output_resolution, self.face_bbox, concat_full=concat_full, sound_filenames=sound_filenames)
        dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=12, pin_memory=True)
        print(len(dataset))
        bar = Bar('inference progress', max=len(jpg_filenames))
        for samples, idxs in dataloader:
            output = inference_model(samples)
            preds = torch.softmax(output, 1)
            for i in range(len(samples)): 
                idx = idxs[i]
                inference_results[time_idxs[idx]].append((true_frame_nums[idx], float(preds[i][1])))
                bar.next()
        bar.finish()
        max_len = 0
        for i in range(len(inference_results)):
            # sort each second by "true" frame number
            inference_results[i] = sorted(inference_results[i], key=lambda item:item[0])
            inference_results[i] = [res[1] for res in inference_results[i]]
            if len(inference_results[i]) > max_len:
                max_len = len(inference_results[i])
        # mean padding
        for i in range(len(inference_results)):
            if len(inference_results[i]) < max_len:
                mean = np.mean(inference_results[i])
                while len(inference_results[i]) < max_len:
                    inference_results[i].append(mean)
        self.inference_results = inference_results
        shutil.rmtree(tempdir)

    def _drawtext(self, stream, second, second_preds):
        chunks = len(second_preds)
        chunksiz = 1.0/chunks
        for j in range(chunks):
            pred = second_preds[j]
            if np.isnan(pred):
                continue
            start = second + j*chunksiz
            end = start + chunksiz
            red = int(255*pred)
            green = int(255*(1.0-pred))
            fontcolor=f'{red:02x}{green:02x}00'
            x = 1920//2 - self.box_width//2
            stream = stream.drawtext(text=f"salt: {pred:.3f}", x=x, y=920, fontsize=48, fontcolor=fontcolor, enable=f'between(t,{start},{end})')
        return stream
 
    def generate_annotated(self, dest_path):
        assert self.inference_results is not None
        rounded_framerate = int(np.round(self.framerate))

        stream = ffmpeg.input(self.filename)
        audio = stream.audio
        x = 1920//2 - self.box_width//2
        stream = stream.drawbox(x=x, y=900, height=self.box_height, width=self.box_width, color='black', t='fill')
        for i in range(len(self.inference_results)):
            second_preds = self.inference_results[i]
            stream = self._drawtext(stream, i, second_preds)
       #stream = ffmpeg.map_audio(stream, audio_stream)
        stream = ffmpeg.output(audio, stream, dest_path)
        stream = ffmpeg.overwrite_output(stream)
        ffmpeg.run(stream)

    def bin(self, bin_size=5):
        assert self.inference_results is not None
        bins = list()
        for i in range(0, len(self.inference_results), bin_size):
            window = list()
            for j in range(i, i+bin_size):
                if j >= len(self.inference_results):
                    break
                window += self.inference_results[j]
            mean = np.mean(window)
            # assert mean is not np.nan
            bins.append((i, mean))
        self.bins = bins

    # some voodoo from the ffmpeg python github
    # start and end are TIMES (in seconds), not FRAMES
    def _trim(self, dest, start, end):
        input_stream = ffmpeg.input(self.filename)
        print(start, end)
        # TODO: this part is exceptionally slow... seems like ffmpeg is
        # processing all frames and then dropping the irrelevant ones
        # when we just need 5-15 seconds of frames processed
        vid = (
            input_stream.video
            .trim(start=start, end=end)
            .setpts('PTS-STARTPTS')
        )
        x = 1920//2 - self.box_width//2
        vid = vid.drawbox(x=x, y=900, height=self.box_height, width=self.box_width, color='black', t='fill')

        for i in range(start, end):
            second_preds = self.inference_results[i]
            vid = self._drawtext(vid, i-start, second_preds)
             
        aud = (
            input_stream.audio
            .filter_('atrim', start=start, end=end)
            .filter_('asetpts', 'PTS-STARTPTS')
        )
    
        joined = ffmpeg.concat(vid, aud, v=1, a=1).node
        output = ffmpeg.output(joined[0], joined[1], dest)
        output = ffmpeg.overwrite_output(output)
        output.run() 

    def _concat_highlights(self, paths, output_path):
        tempfile = 'highlightconcatlist'
        with open(tempfile, 'w') as f:
            for path in paths:
                f.write(f'file \'{path}\'\n')
        (
        ffmpeg
        .input(tempfile, format='concat', safe=0)
        .output(output_path, c='copy')
        .overwrite_output()
        .run()
        )


    # TODO: avoid having to pass bin size to this function?
    def generate_highlights(self, bin_size=5, adjacent=True, percentile=0.995, threshold=0.500, output_path='output.mp4', delete_temp=False):
        tempdir = 'tempclips/'
        if not os.path.exists(tempdir):
            os.makedirs(tempdir)
        n_bins = len(self.bins)
        basename = os.path.splitext(os.path.basename(self.filename))[0] + '_h'
        # sorted by percentile
        threshold_bins = [item for item in self.bins if item[1] > threshold]
        top_bins = sorted(threshold_bins, key=lambda item:item[1], reverse=True)
        # already output bin (times)
        processed = set()
        rounded_framerate = int(self.framerate)
        max_idx = max(int((1.0-percentile)*n_bins), 1)
        selected_bins = sorted(top_bins[:max_idx], key=lambda item:item[0])
        print(selected_bins)
        temp_clips = list()
        for i, b in enumerate(selected_bins):
            if b[0] in processed:
                continue
            else:
                start_time = b[0]
                end_time = min(bin_size*(n_bins-1), start_time + bin_size)
                if adjacent:
                    start_time = max(0, start_time - bin_size)
                    while start_time in processed:
                        start_time += bin_size
                    end_time = min(bin_size*(n_bins-1), end_time + bin_size)

                if end_time - start_time <= 0:
                    continue
                    
                print(start_time, end_time)
                start_frame = rounded_framerate*start_time
                end_frame = rounded_framerate*end_time
                print(start_frame, end_frame)
                dest = os.path.join(tempdir, f'{basename}{i}.mp4')
                self._trim(dest, start=start_time, end=end_time)
                temp_clips.append(dest)
                for t in range(start_time, end_time, bin_size):
                    processed.add(t)
        
        self._concat_highlights(temp_clips, output_path)

        if delete_temp:
            for temp_clip_path in temp_clips:
                os.unlink(temp_clip_path)
        
    def generate_data(self, dest_path):
        # basically don't use this, frame by frame is too goddamn slow
        raise Exception
        pos_path = os.path.join(dest_path, '1')
        neg_path = os.path.join(dest_path, '0')
        if not os.path.exists(pos_path):
            os.makedirs(pos_path)
        if not os.path.exists(neg_path):
            os.makedirs(neg_path)
        for i in range(0, self.nb_frames, FRAMESKIP):
            print(i)
            time = i/self.framerate 
            label = 0 
            for interval in self.positive_segments:
                if time >= interval[0] and time <= interval[1]:
                    label = 1
                    break
                elif time > self.positive_segments[-1][1]:
                    break
            self.read_frame_as_jpg(i)

    def to_row(self):
        row = list()
        row.append(self.filename)
        row.append(self.face_bbox)
        for segment in self.positive_segments:
            row.append(segment)
        return row

    def print_summary(self):
        print(self.filename)
        print(self.height, self.width)
        print(self.framerate)
        print(self.duration)
        print(self.positive_segments)


def load_clip_from_csv_row(row):
    positive_segments = list()
    for i, item in enumerate(row):
        if i == 0:
            filename = item
        elif i == 1:
            face_bbox = ast.literal_eval(item)
        else:
            segment = ast.literal_eval(item)
            assert segment[0] <= segment[1]
            if len(positive_segments):
                assert positive_segments[-1][1] <= segment[0]
            positive_segments.append(segment)

    return Clip(filename, positive_segments, face_bbox)


def main():
    #with open('data.csv', 'w') as csvfile:
    #    csvwriter = csv.writer(csvfile, delimiter=' ')
    #    for clip in clips:
    #        csvwriter.writerow(clip.to_row())
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", help="output data directory", default='data')
    parser.add_argument("--sound", help="sound", action='store_true')
    parser.add_argument("--concat-full", help="concat full frame", action='store_true')
    parser.add_argument("-r", "--resolution", help="resolution", default=256, type=int)
    args = parser.parse_args()

    filenames = set()
    clips = list()
    with open('data.csv', 'r') as csvfile:
        csvreader = csv.reader(csvfile, delimiter=' ')
        for row in csvreader:
            clip = load_clip_from_csv_row(row)
            assert clip.filename not in filenames
            filenames.add(clip.filename)
            clip.print_summary()
            clips.append(clip)
    
    for i in range(0, len(clips)):
        clips[i].print_summary()
        if i < 40:
            if i == 4 or i == 38:
                clips[i].generate_data2(os.path.join(args.d, 'val'),
                                        use_sound=args.sound,
                                        output_resolution=args.resolution,
                                        concat_full=args.concat_full)
            else:
                clips[i].generate_data2(os.path.join(args.d, 'train'),
                                        use_sound=args.sound,
                                        output_resolution=args.resolution,
                                        concat_full=args.concat_full)
        # want determinism in generating datasets to support partial re-generation
        # / non-destructive over-writing
        elif i % 4 == 0:
            clips[i].generate_data2(os.path.join(args.d, 'val'),
                                    use_sound=args.sound,
                                    output_resolution=args.resolution,
                                    concat_full=args.concat_full)
        else:
            clips[i].generate_data2(os.path.join(args.d, 'train'),
                                    use_sound=args.sound,
                                    output_resolution=args.resolution,
                                    concat_full=args.concat_full)


if __name__ == '__main__':
    main() 
