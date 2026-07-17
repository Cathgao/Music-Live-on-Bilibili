#coding:utf-8

import os
import sys
import time
import random
import json
import signal
import threading
import selectors
import subprocess
import audioread
import mutagen
import pysubs2
import service.AssMaker


def _resolve_temp_path(project_path):
    """决定临时目录: 优先 /dev/shm tmpfs, 降级到 <项目>/temp。"""
    _tmp_default = "/dev/shm/Music-Live-on-Bilibili-temp/"
    try:
        os.makedirs(_tmp_default, exist_ok=True)
        return _tmp_default
    except OSError as e:
        print(f"警告: 无法创建 {_tmp_default} ({e}),回退到 {os.path.join(project_path, 'temp')}")
        fallback = os.path.join(project_path, 'temp')
        os.makedirs(fallback, exist_ok=True)
        return fallback


# --- 配置加载 ---
try:
    config = json.load(open('Config.json', encoding='utf-8'))
    path = os.path.abspath(config['path'])
    rtmp = config['rtmp']['url']
    live_code = config['rtmp']['code']
    bitrate = int(config['rtmp']['bitrate'])
    temp_path = _resolve_temp_path(path)

    # 切歌标志文件路径 (与 Danmu.py 中的 skip_flag_file 保持一致)
    skip_flag_file = os.path.join(temp_path, '.skip_current')
    # 当前曲目标题字幕文件 (从 default.ass 衍生)
    temp_ass_path = os.path.join(temp_path, 'temp.ass')

    nightvideo = bool(int(config['nightvideo']['use']))
    rtmp_url = rtmp + live_code
except FileNotFoundError:
    print("错误: Config.json 未找到。请确保配置文件存在。")
    sys.exit(1)
except KeyError as e:
    print(f"错误: Config.json 缺少键 {e}。")
    sys.exit(1)


# --- 常量 ---
AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac')


# ============================================================================
# 辅助函数
# ============================================================================

def get_audio_title(filepath):
    """使用 mutagen 智能获取音频文件的标题。"""
    try:
        audio = mutagen.File(filepath)
        if audio is None:
            return f"文件无法加载: {filepath}"

        if 'TIT2' in audio:
            return audio['TIT2'].text[0]
        if 'title' in audio:
            return audio['title'][0]

        return "no title"
    except Exception as e:
        print(f"get_audio_title 发生错误: {e}")
        return "no title"


def get_audio_length(filepath):
    """读取音频长度(秒)。失败返回 0。"""
    try:
        with audioread.audio_open(filepath) as audio_file:
            seconds = int(audio_file.duration)
        if seconds is not None and seconds > 0:
            return seconds
        print(f'无法获取音频长度: {filepath}')
        return 0
    except Exception as e:
        print(f'读取音频长度错误: {filepath}: {e}')
        return 0


def modify_ass_by_title(output_path, new_text):
    """
    修改 ASS 文件, 为当前画面加上歌名。
    若 default.ass 缺失则创建一个最小 ASS 文件, 避免 ffmpeg 报错。
    """
    try:
        default_ass_path = os.path.join(path, 'default.ass')
        subs = pysubs2.load(default_ass_path, encoding="utf-8")
        new_line = pysubs2.SSAEvent(layer=2, start=0, end=3600000, text=new_text, style='Title')
        subs.append(new_line)
        subs.save(output_path)
        print(f"修改后的 ASS 文件已保存到: {output_path}")
    except FileNotFoundError:
        print(f"警告: 找不到文件 {default_ass_path}。将创建仅含标题的 ASS 文件。")
        subs = pysubs2.SSAFile()
        subs.styles['Title'] = pysubs2.SSAStyle(
            fontname='Arial', fontsize=24, primarycolor=pysubs2.Color(255, 255, 255)
        )
        new_line = pysubs2.SSAEvent(layer=2, start=0, end=3600000, text=new_text, style='Title')
        subs.append(new_line)
        subs.save(output_path)
        print(f"已创建仅含标题的 ASS 文件到: {output_path}")
    except Exception as e:
        print(f"处理 ASS 文件时发生错误: {e}")


def convert_time(n):
    """秒 -> '00:MM:SS'。"""
    s = n % 60
    m = int(n / 60)
    return '00:' + "%02d" % m + ':' + "%02d" % s


def remove_v(filename):
    """异步删除放完的视频及其附属 .ass / .info 文件。"""
    try:
        os.remove(os.path.join(path, 'resource', 'playlist', filename))
    except Exception as e:
        print(e)
    try:
        base_name = os.path.splitext(filename)[0]
        os.remove(os.path.join(path, 'resource', 'playlist', base_name + 'ok.ass'))
        os.remove(os.path.join(path, 'resource', 'playlist', base_name + 'ok.info'))
    except Exception as e:
        print(e)
        print('delete error')


def listdir_cached(directory, state):
    try:
        current_mtime = os.stat(directory).st_mtime
    except OSError:
        current_mtime = 0
    if current_mtime != state.get('mtime'):
        try:
            state['files'] = os.listdir(directory)
            state['mtime'] = current_mtime
        except OSError as e:
            print(f"listdir {directory} 失败: {e}")
            state['files'] = []
            state['mtime'] = current_mtime
    return state.get('files', [])


# ============================================================================
# 进程管理
# ============================================================================

def _kill_pg(pgid, reason=""):
    """
    关闭整组 ffmpeg 进程
    先 SIGTERM, 1 秒后 SIGKILL 兜底。
    """
    if not pgid:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        print(f"进程组 (PGID: {pgid}) 已发送 SIGTERM ({reason})")
    except ProcessLookupError:
        return
    except Exception as e:
        print(f"SIGTERM 失败 (PGID: {pgid}): {e}")


def _monitor_io(process, tag):
    """
    单线程监控子进程的 stderr (用 selectors)。
    这里用 selectors 在一个 daemon 线程里读取 stderr,线程数减半,
    并随主线程退出自动清理 (daemon=True)。
    """
    if not process.stderr:
        return

    sel = selectors.DefaultSelector()
    sel.register(process.stderr, selectors.EVENT_READ)

    try:
        while sel.get_map():
            # 1 秒超时, 期间被 SIGTERM 打断时也能快速退出
            events = sel.select(timeout=1.0)
            for key, _ in events:
                try:
                    line = key.fileobj.readline()
                except (ValueError, OSError):
                    sel.unregister(key.fileobj)
                    continue
                if not line:
                    sel.unregister(key.fileobj)
                    continue
                text = line.decode('utf-8', errors='ignore').strip()
                if text:
                    print(f"[{tag}]: {text}", flush=True)
            # 进程已退出且无更多输出 -> 退出循环
            if process.poll() is not None and not sel.get_map():
                break
    except Exception as e:
        print(f"[{tag}] monitor 异常: {e}")
    finally:
        try:
            sel.close()
        except Exception:
            pass
    print(f"--- {tag} 监控线程已退出 ---", flush=True)


def start_pusher(rtmp_url):
    """
    启动推流器 (Pusher, 进程 1)。
    """
    clean_rtmp_url = rtmp_url.strip('"')
    base_cmd = [
        'ffmpeg',
        # '-loglevel', 'error',
        # '-use_wallclock_as_timestamps', '1',
        '-fflags', '+genpts+igndts',
        '-f', 'mpegts',
        '-i', '-',
        '-c:a', 'copy',
        '-c:v', 'copy',
        '-fps_mode', 'passthrough',
        '-max_delay', '3000000',
        '-use_wallclock_as_timestamps', '1',
        '-f', 'flv',
        clean_rtmp_url,
    ]
    cmd = _with_loglevel(base_cmd)

    print(f"--- 启动推流器 (进程 1) ---\n{' '.join(cmd)}\n", flush=True)

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    threading.Thread(target=_monitor_io, args=(process, 'Pusher'), daemon=True).start()

    return process


def _force_kill_handler(proc, reason=""):
    if not proc or proc.poll() is not None:
        return
    pid = proc.pid
    print(f"Handler 进程组 {pid} {reason},开始终止整组进程...", flush=True)
    try:
        os.killpg(pid, signal.SIGTERM)
        try:
            proc.wait(timeout=1)
            print(f"进程组 (PGID: {pid}) 已优雅退出。", flush=True)
            return
        except subprocess.TimeoutExpired:
            pass
        os.killpg(pid, signal.SIGKILL)
        print(f"进程组 (PGID: {pid}) 强制终止完成。", flush=True)
    except ProcessLookupError:
        pass
    except Exception as kill_e:
        print(f"终止进程组 (PGID: {pid}) 失败: {kill_e}")
        # 降级: 直接 kill 主进程 (不会清理子进程)
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file):
    """
    进程 2: 处理器 (Handler)。
    主循环里调用此函数, 一首歌曲 / 视频对应一次 Handler。
    """
    print(f"--- 启动处理器 (进程 2) ---\n{' '.join(ffmpeg_cmd)}\n", flush=True)
    process = None
    handler_stdin = None

    try:
        # 清空旧的 skip 标志文件 (可能上一次未清理)
        if os.path.exists(skip_flag_file):
            try:
                os.remove(skip_flag_file)
            except Exception:
                pass

        # Linux: start_new_session=True 让 ffmpeg 进入新的进程组,
        # 以便后续用 os.killpg 终止整棵进程树 (等价于 Windows 的 taskkill /T)
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        handler_stdin = process.stdin
        threading.Thread(target=_monitor_io, args=(process, 'Handler'), daemon=True).start()

        print("开始将数据流式传输到 Pusher...", flush=True)

        skip_requested = False
        while True:
            # 检查切歌标志文件
            if os.path.exists(skip_flag_file) and not skip_requested:
                print("[切歌] 检测到切歌信号,正在发送优雅停止命令给 Handler...", flush=True)
                skip_requested = True

                if handler_stdin:
                    try:
                        handler_stdin.write(b'q\n')
                        handler_stdin.flush()
                        print("[切歌] 已发送优雅停止命令。等待 Handler 完成...", flush=True)
                    except (BrokenPipeError, IOError) as e:
                        print(f"[切歌] 无法发送停止命令: {e},尝试强制终止...", flush=True)
                        _force_kill_handler(process, "无法发送 'q' 命令")

                try:
                    os.remove(skip_flag_file)
                except Exception:
                    pass

            data = process.stdout.read(65536)
            if not data:
                if skip_requested:
                    print("[切歌] Handler 已完成优雅停止。", flush=True)
                else:
                    print("Handler 已完成正常播放。", flush=True)
                break

            try:
                pusher_stdin.write(data)
                try:
                    pusher_stdin.flush()
                except (BrokenPipeError, IOError):
                    pass
            except BrokenPipeError:
                print("错误: 管道已损坏。推流器 (进程 1) 可能已崩溃。", flush=True)
                if process.poll() is None:
                    try:
                        if handler_stdin:
                            handler_stdin.write(b'q\n')
                            handler_stdin.flush()
                        process.wait(timeout=2)
                    except Exception:
                        _force_kill_handler(process, "在 BrokenPipeError 后未在 2 秒内响应 'q'")
                raise

        # 确保 Handler 进程正常退出 (处理正常播放结束的情况)
        if process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _force_kill_handler(process, "播放结束但未在 3 秒内退出")

        stderr_output = process.stderr.read().decode('utf-8', errors='ignore')

        print(f"--- 处理器 (进程 2) 已完成 (RC: {process.returncode}) ---", flush=True)

        if process.returncode not in (0, 255, -15, -2):
            print("FFmpeg (进程 2) 错误输出:")
            print(stderr_output)

    except BrokenPipeError:
        # main 循环会重启 pusher; 这里只是被记录
        raise
    except Exception as e:
        print(f"stream_to_pusher 发生严重错误: {e}", flush=True)
        if process and process.poll() is None:
            try:
                if handler_stdin:
                    handler_stdin.write(b'q\n')
                    handler_stdin.flush()
                process.wait(timeout=2)
            except Exception:
                _force_kill_handler(process, "在严重错误后未在 2 秒内响应 'q'")
        raise
    finally:
        if handler_stdin:
            try:
                handler_stdin.close()
            except Exception:
                pass


# ============================================================================
# ffmpeg 命令构造 (list 形式, shell=False)
# ============================================================================

_FF_LOGLEVEL = ['-loglevel', 'warning', '-nostats']


def _with_loglevel(cmd):
    """在 'ffmpeg' 之后立刻插入 _FF_LOGLEVEL。"""
    assert cmd and cmd[0] == 'ffmpeg'
    return [cmd[0]] + _FF_LOGLEVEL + cmd[1:]


def _v4l2m2m_args():
    """
    公共 v4l2m2m (Pi 硬件编码器) 参数: CBR, GOP, bufsize, 关键标志。
    """
    return [
        '-c:v', 'h264_v4l2m2m',
        '-b:v', f'{bitrate}k',
        '-maxrate', f'{bitrate + 300}k',
        '-minrate', f'{max(100, bitrate - 300)}k',
        '-bufsize', f'{bitrate * 2}k',
        '-g', '5',
        '-pix_fmt', 'yuv420p',
    ]


def _audio_args():
    """公共音频参数: 48kHz aac 320k。"""
    return [
        '-af', 'aformat=sample_rates=48000',
        '-c:a', 'aac',
        '-b:a', '320k',
    ]


def _ts_output_args():
    """输出到 mpegts, 关键: -use_wallclock_as_timestamps 让时戳对齐墙钟。"""
    return [
        '-bsf:v', 'h264_mp4toannexb',
        '-use_wallclock_as_timestamps', '1',
        '-f', 'mpegts', '-',
    ]


def cmd_night_audio(pic_path, audio_path, ass_path, seconds):
    """夜间模式: 静图 + 音频 + ass 字幕。"""
    return _with_loglevel([
        'ffmpeg',
        '-threads', '1',
        '-re',
        '-loop', '1', '-r', '5',
        '-t', str(int(seconds)),
        '-f', 'image2', '-i', pic_path,
        '-i', audio_path,
        '-vf', f'ass={ass_path}',
        *_v4l2m2m_args(),
        *_audio_args(),
        *_ts_output_args(),
    ])


def cmd_playlist_audio(cover_path, audio_path, ass_path, seconds):
    """点播音频: 封面 + 音频 + (可选) ass 字幕。"""
    cmd = [
        'ffmpeg',
        '-threads', '1',
        '-re',
        '-loop', '1', '-r', '5',
        '-t', str(int(seconds)),
        '-f', 'image2', '-i', cover_path,
        '-i', audio_path,
    ]
    if ass_path:
        cmd += ['-vf', f'ass={ass_path}']
    cmd += _v4l2m2m_args()
    cmd += _audio_args()
    cmd += _ts_output_args()
    return _with_loglevel(cmd)


def cmd_playlist_flv(flv_path):
    """点播视频 (FLV): 直接 remux, 不重编码。"""
    return _with_loglevel([
        'ffmpeg',
        '-threads', '1',
        '-i', flv_path,
        '-af', 'aformat=sample_rates=48000',
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '320k',
        '-f', 'flv', '-',
    ])


def cmd_music_cover_ass(pic_path, jpg_path, audio_path, ass_path, seconds):
    """垫片音乐: 背景图 + 封面 + 音频 + ass 字幕 (filter_complex overlay)。"""
    return _with_loglevel([
        'ffmpeg',
        '-threads', '1',
        '-re',
        '-loop', '1', '-r', '5',
        '-t', str(int(seconds)),
        '-f', 'image2', '-i', pic_path,
        '-i', jpg_path,
        '-filter_complex', f'[0:v][1:v]overlay=30:390[cover];[cover]ass={ass_path}',
        '-i', audio_path,
        '-map', '[cover]',
        '-map', '2:a',
        *_v4l2m2m_args(),
        *_audio_args(),
        *_ts_output_args(),
    ])


def cmd_music_ass(pic_path, audio_path, ass_path, seconds):
    """垫片音乐: 背景图 + 音频 + ass 字幕 (简单覆盖)。"""
    return _with_loglevel([
        'ffmpeg',
        '-threads', '1',
        '-re',
        '-loop', '1', '-r', '5',
        '-t', str(int(seconds)),
        '-f', 'image2', '-i', pic_path,
        '-i', audio_path,
        '-vf', f'ass={ass_path}',
        *_v4l2m2m_args(),
        *_audio_args(),
        *_ts_output_args(),
    ])


def cmd_music_title(pic_path, audio_path, ass_path, seconds):
    """垫片音乐: 背景图 + 音频 + 标题字幕 (modify_ass_by_title 生成的临时 ass)。"""
    return _with_loglevel([
        'ffmpeg',
        '-threads', '1',
        '-re',
        '-loop', '1', '-r', '5',
        '-t', str(int(seconds)),
        '-f', 'image2', '-i', pic_path,
        '-i', audio_path,
        '-vf', f'ass={ass_path}',
        *_v4l2m2m_args(),
        *_audio_args(),
        *_ts_output_args(),
    ])


def cmd_music_flv(flv_path):
    """垫片视频 (FLV): 直接 remux。"""
    return _with_loglevel([
        'ffmpeg',
        '-threads', '1',
        '-i', flv_path,
        '-af', 'aformat=sample_rates=48000',
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '320k',
        '-f', 'flv', '-',
    ])


# ============================================================================
# 主循环
# ============================================================================

def main():
    pusher_process = None
    pusher_stdin = None

    # 目录扫描缓存 (mtime-based)
    cache_playlist = {'mtime': 0, 'files': []}
    cache_night = {'mtime': 0, 'files': []}
    cache_pic = {'mtime': 0, 'files': []}
    cache_music = {'mtime': 0, 'files': []}

    def _signal_to_keyboard_interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _signal_to_keyboard_interrupt)
        signal.signal(signal.SIGINT, _signal_to_keyboard_interrupt)
    except (ValueError, OSError) as sig_e:
        # 主线程外的线程注册会抛 ValueError; 忽略即可
        print(f"信号注册失败 (非致命): {sig_e}")

    try:
        pusher_process = start_pusher(rtmp_url)
        pusher_stdin = pusher_process.stdin
        print("推流器已启动。主循环开始。", flush=True)

        # 主循环 (进程 2 的管理器)
        while True:
            # 循环开始时, 检查 Pusher 状态
            if pusher_process and pusher_process.poll() is not None:
                print(
                    f"主循环检测到推流器 (进程 1) 已退出 (RC: {pusher_process.returncode})。"
                    "正在尝试重新启动...", flush=True
                )
                time.sleep(2)
                pusher_process = start_pusher(rtmp_url)
                pusher_stdin = pusher_process.stdin
                print("推流器已重新启动,继续主循环。", flush=True)
                continue

            try:
                # --- 夜间模式 ---
                if (time.localtime()[3] <= 5) and nightvideo:
                    print('night is coming~', flush=True)
                    night_dir = os.path.join(path, 'resource', 'night')
                    night_files = listdir_cached(night_dir, cache_night)
                    if not night_files:
                        print("夜间文件夹为空,跳过")
                        time.sleep(60)
                        continue

                    night_files.sort()
                    night_ran = random.randint(0, len(night_files) - 1)
                    selected_file = night_files[night_ran]
                    full_file_path = os.path.join(night_dir, selected_file)

                    if selected_file.endswith(AUDIO_EXTENSIONS):
                        pic_dir = os.path.join(path, 'resource', 'img')
                        pic_files = listdir_cached(pic_dir, cache_pic)
                        if not pic_files:
                            print("图片文件夹为空,跳过夜间模式")
                            time.sleep(60)
                            continue
                        pic_files.sort()
                        pic_ran = random.randint(0, len(pic_files) - 1)
                        pic_path = os.path.join(pic_dir, pic_files[pic_ran])
                        seconds = get_audio_length(full_file_path)
                        print(f'audio long: {convert_time(seconds)}', flush=True)

                        base_name = os.path.splitext(selected_file)[0]
                        ass_path = os.path.join(night_dir, base_name + '.ass')
                        if not os.path.isfile(ass_path):
                            service.AssMaker.make_ass(
                                base_name,
                                '当前是晚间专属时间哦~时间范围: 凌晨 0-5 点\\N'
                                '大家晚安哦~做个好梦~\\N'
                                f'当前文件名: {selected_file}',
                                path,
                            )
                        ffmpeg_cmd = cmd_night_audio(pic_path, full_file_path, ass_path, seconds)
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)
                    continue

                # --- 点播播放列表 ---
                playlist_dir = os.path.join(path, 'resource', 'playlist')

                while True:
                    files = listdir_cached(playlist_dir, cache_playlist)
                    files.sort()
                    count = 0
                    selected_file_to_play = None

                    for f in files:
                        full_file_path = os.path.join(playlist_dir, f)

                        # 音频 (排除 .download 中间态)
                        if f.endswith(AUDIO_EXTENSIONS) and (f.find('.download') == -1):
                            try:
                                seconds = get_audio_length(full_file_path)
                                if seconds == 0:
                                    print('无法获取音频长度,跳过该文件', flush=True)
                                    continue
                            except Exception as e:
                                print(e, flush=True)
                                continue

                            selected_file_to_play = f
                            count = 1
                            break

                        # 视频 FLV (ok 标记 + 排除渲染中)
                        if (f.find('ok.flv') != -1) and (f.find('.download') == -1) and (f.find('rendering') == -1):
                            selected_file_to_play = f
                            count = 2
                            break

                    if count == 0:
                        break  # 进入垫片

                    f = selected_file_to_play
                    full_file_path = os.path.join(playlist_dir, f)

                    if count == 1:  # 音频
                        base_name = os.path.splitext(f)[0]
                        ass_path = os.path.join(playlist_dir, base_name + '.ass')
                        cover_path = os.path.join(playlist_dir, base_name + '.jpg')
                        info_path = os.path.join(playlist_dir, base_name + '.info')
                        ass_path_use = ass_path if os.path.exists(ass_path) else None
                        ffmpeg_cmd = cmd_playlist_audio(cover_path, full_file_path, ass_path_use, seconds)
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)

                        # 播放完后清理
                        try:
                            if os.path.exists(info_path):
                                os.remove(info_path)
                            if os.path.exists(ass_path):
                                os.remove(ass_path)
                            if os.path.exists(cover_path):
                                os.remove(cover_path)
                            if os.path.exists(full_file_path):
                                os.remove(full_file_path)
                            print(f"成功删除播放列表音频文件: {f}", flush=True)
                        except Exception as e:
                            print(f'delete error after playing: {e}', flush=True)

                    elif count == 2:  # 视频
                        print(f'flv: {f}', flush=True)
                        ffmpeg_cmd = cmd_playlist_flv(full_file_path)
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)

                        new_name = f.replace("ok", "")
                        os.rename(full_file_path, os.path.join(playlist_dir, new_name))
                        threading.Thread(target=remove_v, args=(new_name,), daemon=True).start()

                # --- 垫片 (count == 0) ---
                if count == 0:
                    print('no media', flush=True)
                    music_dir = os.path.join(path, 'resource', 'music')
                    mp3_files = listdir_cached(music_dir, cache_music)
                    if not mp3_files:
                        print("音乐文件夹为空,等待")
                        time.sleep(60)
                        continue

                    mp3_files.sort()
                    mp3_ran = random.randint(0, len(mp3_files) - 1)
                    selected_file = mp3_files[mp3_ran]
                    full_file_path = os.path.join(music_dir, selected_file)

                    if selected_file.endswith(AUDIO_EXTENSIONS):
                        pic_dir = os.path.join(path, 'resource', 'img')
                        pic_files = listdir_cached(pic_dir, cache_pic)
                        if not pic_files:
                            print("图片文件夹为空,跳过")
                            time.sleep(60)
                            continue
                        pic_files.sort()
                        pic_ran = random.randint(0, len(pic_files) - 1)
                        pic_path = os.path.join(pic_dir, pic_files[pic_ran])

                        seconds = get_audio_length(full_file_path)
                        title = get_audio_title(full_file_path)
                        print(f'mp3 title: {title} long: {int(seconds)}', flush=True)

                        base_name = os.path.splitext(selected_file)[0]
                        ass_path = os.path.join(music_dir, base_name + '.ass')
                        jpg_path = os.path.join(music_dir, base_name + '.jpg')

                        if os.path.isfile(ass_path):
                            if os.path.isfile(jpg_path):
                                ffmpeg_cmd = cmd_music_cover_ass(
                                    pic_path, jpg_path, full_file_path, ass_path, seconds
                                )
                            else:
                                ffmpeg_cmd = cmd_music_ass(
                                    pic_path, full_file_path, ass_path, seconds
                                )
                        else:
                            # 无 ass: 临时生成带标题的字幕
                            modify_ass_by_title(temp_ass_path, title)
                            ffmpeg_cmd = cmd_music_title(
                                pic_path, full_file_path, temp_ass_path, seconds
                            )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)

                    if selected_file.find('.flv') != -1:
                        ffmpeg_cmd = cmd_music_flv(full_file_path)
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)

            except BrokenPipeError:
                print("主循环检测到管道破坏!推流器已退出。正在尝试重新启动...", flush=True)
                if pusher_process and pusher_process.poll() is None:
                    pusher_process.kill()
                time.sleep(2)
                pusher_process = start_pusher(rtmp_url)
                pusher_stdin = pusher_process.stdin
                print("推流器已重新启动,继续主循环。", flush=True)

            except Exception as e:
                print(f"主循环发生严重错误: {e}", flush=True)
                print("假定推流器或处理器状态不稳定。正在强制重启推流器...", flush=True)
                if pusher_process and pusher_process.poll() is None:
                    try:
                        pusher_process.kill()
                    except Exception as kill_e:
                        print(f"尝试杀死旧的 Pusher 失败: {kill_e}")
                print("5 秒后重试...", flush=True)
                time.sleep(5)
                try:
                    pusher_process = start_pusher(rtmp_url)
                    pusher_stdin = pusher_process.stdin
                    print("推流器已重新启动,继续主循环。", flush=True)
                except Exception as restart_e:
                    print(f"致命错误: 重启推流器失败: {restart_e}", flush=True)
                    time.sleep(10)

    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C。正在关闭...", flush=True)
    except Exception as e:
        print(f"致命错误: 推流器无法启动或主循环崩溃: {e}", flush=True)
    finally:
        print("清理...", flush=True)
        if pusher_stdin:
            try:
                pusher_stdin.close()
            except Exception:
                pass
        if pusher_process and pusher_process.poll() is None:
            try:
                os.killpg(pusher_process.pid, signal.SIGTERM)
                try:
                    pusher_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(pusher_process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"终止推流器进程组失败: {e}")
                try:
                    pusher_process.kill()
                except Exception:
                    pass
        print("所有进程已关闭。", flush=True)


if __name__ == "__main__":
    print(f"使用的工作路径: {path}", flush=True)
    main()