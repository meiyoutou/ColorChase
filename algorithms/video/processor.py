import os
import cv2
import numpy as np
import tempfile
import subprocess
from ..color_transfer import transfer_color


def _get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def is_key_frame(prev_frame, curr_frame, frame_idx, interval=30, cache=None, custom_keyframes=None):
    if cache is None:
        cache = {}
    if frame_idx == 0:
        return "first"
    if custom_keyframes is not None and len(custom_keyframes) > 0:
        if frame_idx in custom_keyframes:
            return "interval"
    elif frame_idx % interval == 0:
        return "interval"

    h, w = curr_frame.shape[:2]
    hist_size = 50
    h_ranges = [0, 180]
    s_ranges = [0, 256]

    curr_hsv = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2HSV)
    curr_h = cv2.calcHist([curr_hsv], [0], None, [hist_size], h_ranges)
    curr_s = cv2.calcHist([curr_hsv], [1], None, [hist_size], s_ranges)
    cv2.normalize(curr_h, curr_h, 0, 1, cv2.NORM_MINMAX)
    cv2.normalize(curr_s, curr_s, 0, 1, cv2.NORM_MINMAX)
    curr_hist = np.concatenate([curr_h.flatten(), curr_s.flatten()])

    cache_key = id(prev_frame)
    if cache_key not in cache:
        prev_hsv = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2HSV)
        prev_h = cv2.calcHist([prev_hsv], [0], None, [hist_size], h_ranges)
        prev_s = cv2.calcHist([prev_hsv], [1], None, [hist_size], s_ranges)
        cv2.normalize(prev_h, prev_h, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(prev_s, prev_s, 0, 1, cv2.NORM_MINMAX)
        prev_hist = np.concatenate([prev_h.flatten(), prev_s.flatten()])
        cache[cache_key] = prev_hist

    if cache_key in cache:
        prev_hist = cache[cache_key]
    else:
        prev_hist = curr_hist

    corr = cv2.compareHist(curr_hist.astype(np.float32), prev_hist.astype(np.float32), cv2.HISTCMP_CORREL)

    del cache[cache_key]
    cache[id(curr_frame)] = curr_hist

    if corr < 0.8:
        return "scene"

    return None


def extract_frames(video_path, output_dir=None, max_frames=None):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="colorchase_frames_")
    os.makedirs(output_dir, exist_ok=True)

    frames = []
    frame_paths = []
    idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frames.append(frame)
        frame_path = os.path.join(output_dir, f"frame_{idx:06d}.png")
        frame_paths.append(frame_path)
        _, buf = cv2.imencode('.png', frame)
        buf.tofile(frame_path)

        idx += 1
        if max_frames and idx >= max_frames:
            break

    cap.release()

    return {
        "frames": frames,
        "frame_paths": frame_paths,
        "output_dir": output_dir,
        "fps": fps,
        "width": width,
        "height": height,
        "total_frames": len(frames),
    }


def transfer_video_frames(frames, reference_img, algorithm="luminance_partition",
                          key_frame_interval=30, transition_frames=5,
                          progress_callback=None, **kwargs):
    from core.color.lut_extractor import extract_lut_from_pair
    from core.render.full_render import apply_lut

    result_frames = []
    total = len(frames)
    current_lut = None
    target_lut = None
    transition_count = 0
    prev_frame = None

    for i, frame in enumerate(frames):
        key_type = is_key_frame(prev_frame, frame, i, interval=key_frame_interval)

        if key_type is not None:
            if progress_callback:
                progress_callback(i + 1, total, True)
            result = transfer_color(frame, reference_img, algorithm=algorithm, **kwargs)
            try:
                src_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                tgt_rgb = cv2.cvtColor(result, cv2.COLOR_BGR2RGB)
                new_lut = extract_lut_from_pair(src_rgb, tgt_rgb, lut_size=33)
            except Exception:
                new_lut = None

            if key_type in ("first", "scene") or current_lut is None:
                current_lut = new_lut if new_lut is not None else current_lut
                target_lut = None
                transition_count = transition_frames
            else:
                if new_lut is not None and current_lut is not None:
                    target_lut = new_lut
                    transition_count = 0
                else:
                    current_lut = new_lut if new_lut is not None else current_lut
                    target_lut = None
                    transition_count = transition_frames

        if progress_callback:
            progress_callback(i + 1, total, key_type is not None)

        if target_lut is not None and current_lut is not None and transition_count < transition_frames:
            transition_count += 1
            alpha = min(transition_count / transition_frames, 1.0)
            mixed_lut = current_lut * (1.0 - alpha) + target_lut * alpha
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result_rgb = apply_lut(frame_rgb, mixed_lut.astype(np.float32))
            final_bgr = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
            if alpha >= 1.0:
                current_lut = target_lut
                target_lut = None
        elif current_lut is not None:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result_rgb = apply_lut(frame_rgb, current_lut)
            final_bgr = cv2.cvtColor(result_rgb, cv2.COLOR_RGB2BGR)
        else:
            final_bgr = transfer_color(frame, reference_img, algorithm=algorithm, **kwargs)

        result_frames.append(final_bgr)
        prev_frame = frame

    return result_frames


def assemble_video(frames, output_path, fps=30.0, width=None, height=None, audio_source=None):
    if isinstance(frames, str):
        frame_dir = frames
        frame_files = sorted([
            f for f in os.listdir(frame_dir)
            if f.endswith(('.png', '.jpg', '.jpeg'))
        ])
        if not frame_files:
            raise ValueError("没有帧可以组装")
        first_frame_path = os.path.join(frame_dir, frame_files[0])
        first_frame = cv2.imdecode(np.fromfile(first_frame_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if width is None or height is None:
            height, width = first_frame.shape[:2]

        ffmpeg_path = _get_ffmpeg_path()
        cmd = [
            ffmpeg_path, "-y",
            "-framerate", str(fps),
            "-i", os.path.join(frame_dir, "frame_%06d.png"),
        ]
        if audio_source and os.path.exists(audio_source):
            cmd += ["-i", audio_source]
        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        ]
        if audio_source and os.path.exists(audio_source):
            cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest", "-map", "0:v:0", "-map", "1:a:0"]
        cmd.append(output_path)
    try:
        subprocess.run(cmd, check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        stderr_output = ""
        if hasattr(e, 'stderr') and e.stderr:
            stderr_output = e.stderr.decode('utf-8', errors='replace')[:500]
        if audio_source and os.path.exists(audio_source):
            cmd_no_audio = [
                ffmpeg_path, "-y",
                "-framerate", str(fps),
                "-i", os.path.join(frame_dir, "frame_%06d.png"),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                output_path,
            ]
            try:
                subprocess.run(cmd_no_audio, check=True, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                raise RuntimeError(f"ffmpeg assemble failed: {stderr_output}")
        return output_path
    return output_path


def process_video(video_path, reference_img, output_path=None,
                  algorithm="luminance_partition", max_frames=None,
                  key_frame_interval=30, transition_frames=5,
                  progress_callback=None, **kwargs):
    print(f"开始处理视频: {video_path}")

    video_info = extract_frames(video_path, max_frames=max_frames)
    print(f"  提取 {video_info['total_frames']} 帧, "
          f"FPS={video_info['fps']}, "
          f"分辨率={video_info['width']}x{video_info['height']}")

    print(f"  使用算法: {algorithm}")
    result_frames = transfer_video_frames(
        video_info["frames"], reference_img,
        algorithm=algorithm,
        key_frame_interval=key_frame_interval,
        transition_frames=transition_frames,
        progress_callback=progress_callback,
        **kwargs
    )

    if output_path is None:
        base_dir = os.path.dirname(video_path)
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(base_dir, f"{base_name}_colorchased.mp4")

    assemble_video(
        result_frames, output_path,
        fps=video_info["fps"],
        width=video_info["width"],
        height=video_info["height"]
    )

    print(f"  视频已保存: {output_path}")

    for fp in video_info["frame_paths"]:
        if os.path.exists(fp):
            os.remove(fp)

    return output_path
