#coding:utf-8
import os
import sys
import time
import random
import mutagen
import json
import shutil
import _thread
import service.AssMaker
import pysubs2
import subprocess
import os.path
import re
import subprocess
import audioread

# --- 全局配置 ---
try:
    config = json.load(open('Config.json', encoding='utf-8'))
    path = config['path']
    rtmp = config['rtmp']['url']
    live_code = config['rtmp']['code']
    temp_ass_path = os.path.join("R:\\temp\\", 'temp.ass') # 使用工作路径下的临时文件
    temp_path = "R:\\temp\\"
    nightvideo = bool(int(config['nightvideo']['use']))
    # rtmp_url = rtmp + live_code
    rtmp_url = "rtmp://192.168.31.217:1935/livehime"
except FileNotFoundError:
    print("错误：Config.json 未找到。请确保配置文件存在。")
    sys.exit(1)
except KeyError as e:
    print(f"错误：Config.json 缺少键 {e}。")
    sys.exit(1)

# 支持的音频格式
AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac')

def get_audio_title(filepath):
    """
    使用 mutagen 智能获取音频文件的标题。
    """
    try:
        audio = mutagen.File(filepath)
        if audio is None:
            return f"文件无法加载: {filepath}"

        if 'TIT2' in audio:
            return audio['TIT2'].text[0]
        if 'title' in audio:
            return audio['title'][0]

        return "no tile"
    except Exception as e:
        print(f"get_audio_title 发生错误: {e}")
        return "no tile"

def get_audio_length(filepath):
    try:
        with audioread.audio_open(filepath) as audio_file:
            duration = audio_file.duration 
            seconds = int(duration) # 转换为整数秒
        if seconds is not None and seconds > 0:
                return seconds
        else:
            print(f'无法获取音频长度: {filepath}')
            return 0
    except Exception as e:
        print(f'读取音频长度错误: {filepath}: {e}')
              

def modify_ass_by_title(output_path, new_text):
    """
    修改 ASS 文件，为当前画面加上歌名
    """
    try:
        # 使用 os.path.join 确保跨平台路径兼容性
        default_ass_path = os.path.join(path, 'default.ass')
        subs = pysubs2.load(default_ass_path, encoding="utf-8")
        new_line = pysubs2.SSAEvent(layer=2, start=0, end=3600000, text=new_text, style='Title')
        subs.append(new_line)       
        subs.save(output_path)
        print(f"修改后的 ASS 文件已保存到：{output_path}")
    except FileNotFoundError:
        print(f"错误：找不到文件 {default_ass_path}")
    except Exception as e:
        print(f"处理 ASS 文件时发生错误: {e}")

def convert_ass_path_format(windows_path):
    temp_path = windows_path.replace('\\', '/')
    colon_index = temp_path.find(':')

    if colon_index == -1:
        return temp_path
    drive_letter = temp_path[:colon_index]
    rest_of_path = temp_path[colon_index + 1:] 
    if rest_of_path.startswith('/'):
        content_path = rest_of_path[1:]
    else:
        content_path = rest_of_path
    final_output = drive_letter + '\\\\\\:' + content_path
    return final_output
    
def convert_time(n):
    s = n % 60
    m = int(n / 60)
    return '00:' + "%02d" % m + ':' + "%02d" % s

def remove_v(filename):
    """
    移动放完的视频到缓存文件夹
    """
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

def monitor_handler_stderr(process):
    """
    实时监控 Handler 进程的 stderr，防止缓冲区阻塞。
    """
    print("--- 启动处理器错误监控线程 ---")
    while True:
        try:
            # 读取一行 stderr
            line = process.stderr.readline().decode('utf-8', errors='ignore')
            
            # 如果没有更多输出且进程已退出
            if not line and process.poll() is not None:
                break
            
            # 打印非空行
            if line.strip():
                print(f"[Handler Error]: {line.strip()}")
        except Exception:
            # 进程关闭可能导致读取错误
            break
    print("--- 处理器错误监控线程已退出 ---")
    
# --- 核心推流函数 (进程 1) ---
def start_pusher(rtmp_url):
    """
    启动推流器 (进程 1)。
    使用 command string + shell=True 确保 RTMP URL 在 Windows 上正确解析。
    返回推流进程对象。
    """
    # 确保 URL 是纯净的，不包含额外的引号
    clean_rtmp_url = rtmp_url.strip('"') 
    
    # 构造命令字符串，确保 RTMP URL 被引号包裹
    # 添加参数说明：
    # -fflags +igndts: 忽略无效的DTS时间戳（输入选项）
    # -vsync 0: 不对视频帧时间戳进行重新同步（输出选项）
    # -max_delay 5000000: 最大5秒缓冲，防止音频堆积（输出选项）
    cmd_string = (
        f'ffmpeg -fflags +igndts -f mpegts -i - -c:a copy -c:v copy -fps_mode passthrough -vsync 0 -max_delay 5000000 -f flv "{clean_rtmp_url}"'
    )
    
    print(f"--- 启动推流器 (进程 1) ---\n{cmd_string}\n")
    
    # 关键修改：使用 shell=True 并传递 cmd_string
    process = subprocess.Popen(
        cmd_string,
        shell=True, 
        stdin=subprocess.PIPE, 
        stderr=subprocess.PIPE
    )
    
    # 在新线程中监控推流器的 stderr，防止主线程阻塞
    _thread.start_new_thread(monitor_pusher, (process,))
    
    return process

def monitor_pusher(process):
    """
    监控推流器进程的 stderr，并将输出打印出来。
    """
    while True:
        try:
            line = process.stderr.readline().decode('utf-8', errors='ignore')
            if not line and process.poll() is not None:
                break
            if line.strip():
                print(f"[Pusher]: {line.strip()}")
        except Exception as e:
            print(f"监控推流器 stderr 发生错误: {e}")
            break
    print("--- 推流器监控线程已退出 ---")
    
def stream_to_pusher(ffmpeg_command, pusher_stdin, skip_flag_file):
    """
    进程 2：处理器。
    执行 ffmpeg 命令，捕获其 stdout，并将其写入推流器的标准输入 (stdin)。
    支持通过 skip_flag_file 标志文件动态中止 Handler 进程（用于切歌功能）。
    
    关键改进：
    1. 使用 stdin 管道而不是 stdout 来控制 FFmpeg 进程
    2. 切歌时优雅地停止 FFmpeg，而不是强制终止
    3. 确保完整的 MPEG-TS 包被发送到 Pusher
    """
    print(f"--- 启动处理器 (进程 2) ---\n{ffmpeg_command}\n")
    process = None
    handler_stdin = None
    try:
        # 清空旧的 skip 标志文件（如果存在）
        if os.path.exists(skip_flag_file):
            try:
                os.remove(skip_flag_file)
            except Exception:
                pass
        
        # 关键修改：打开 Handler 的 stdin，以便发送 'q' 命令优雅地停止它
        process = subprocess.Popen(ffmpeg_command, shell=True, stdin=subprocess.PIPE, 
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        handler_stdin = process.stdin
        _thread.start_new_thread(monitor_handler_stderr, (process,))
        
        print("开始将数据流式传输到 Pusher...")
        
        skip_requested = False
        while True:
            # 检查切歌标志文件，若存在则优雅地停止 Handler 进程
            if os.path.exists(skip_flag_file) and not skip_requested:
                print("[切歌] 检测到切歌信号，正在发送优雅停止命令给 Handler...")
                skip_requested = True
                
                # 优雅地停止 FFmpeg：发送 'q' 命令到其 stdin
                # 这会让 FFmpeg 正常完成当前帧并输出完整的 MPEG-TS 包
                if handler_stdin:
                    try:
                        handler_stdin.write(b'q\n')
                        handler_stdin.flush()
                        print("[切歌] 已发送优雅停止命令。等待 Handler 完成...")
                    except (BrokenPipeError, IOError) as e:
                        print(f"[切歌] 无法发送停止命令: {e}，尝试强制终止...")
                        if process.poll() is None:
                            process.terminate()
                            time.sleep(0.5)
                            if process.poll() is None:
                                process.kill()
                
                # 清空标志文件
                try:
                    os.remove(skip_flag_file)
                except Exception:
                    pass
            
            # 64k 缓冲区，用于读取 Handler 的输出
            data = process.stdout.read(65536) 
            if not data:
                # Handler 已完成输出
                print("[切歌] Handler 已完成优雅停止。")
                break
            try:
                # 关键修改：将 Handler 的输出写入 Pusher 的 stdin
                pusher_stdin.write(data)
                # 每次写入后尝试刷新缓冲区，确保数据及时传送
                try:
                    pusher_stdin.flush()
                except (BrokenPipeError, IOError):
                    # 忽略刷新时的错误
                    pass
            except BrokenPipeError:
                print("错误：管道已损坏。推流器 (进程 1) 可能已崩溃。")
                # 优雅地停止 Handler
                if process.poll() is None:
                    try:
                        if handler_stdin:
                            handler_stdin.write(b'q\n')
                            handler_stdin.flush()
                        process.wait(timeout=2)
                    except:
                        if process.poll() is None:
                            process.terminate()
                            time.sleep(0.5)
                            if process.poll() is None:
                                process.kill()
                raise
        
        # 确保 Handler 进程正常退出
        if process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                print("警告：Handler 进程未在规定时间内退出，强制终止...")
                process.terminate()
                time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
        
        stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
        
        print(f"--- 处理器 (进程 2) 已完成 (RC: {process.returncode}) ---")
        
        if process.returncode != 0 and process.returncode != 255 and process.returncode != -15 and process.returncode != -2:
            # -15 是 SIGTERM，-2 是 SIGINT（优雅停止的正常退出码）
            print("FFmpeg (进程 2) 错误输出:")
            print(stderr_output)

    except Exception as e:
        print(f"stream_to_pusher 发生严重错误: {e}")
        if process and process.poll() is None:
            try:
                if handler_stdin:
                    handler_stdin.write(b'q\n')
                    handler_stdin.flush()
                process.wait(timeout=2)
            except:
                process.kill()
        raise
    finally:
        if handler_stdin:
            try:
                handler_stdin.close()
            except:
                pass

def main():
    # 1. 启动推流器 (进程 1) 并获取其 stdin
    pusher_process = None
    pusher_stdin = None
    
    # 切歌标志文件路径
    skip_flag_file = os.path.join(temp_path, '.skip_current')

    try:
        pusher_process = start_pusher(rtmp_url)
        pusher_stdin = pusher_process.stdin
        print("推流器已启动。主循程开始。")
            
        # 2. 启动主循环 (进程 2 的管理器)
        while True:
            # ****** 循环开始时，检查 Pusher 状态 ******
            if pusher_process and pusher_process.poll() is not None:
                # Pusher 已退出，无论什么原因，都需要重新启动
                print(f"主循环检测到推流器 (进程 1) 已退出 (RC: {pusher_process.returncode})。正在尝试重新启动...")
                
                # 尝试重新启动推流器
                time.sleep(2)
                pusher_process = start_pusher(rtmp_url)
                pusher_stdin = pusher_process.stdin
                print("推流器已重新启动，继续主循环。")
                
                # 如果 Handler 正在运行，它将写入一个已经关闭的 PIPE
                # 并在下一次 stream_to_pusher 调用时，新启动一个 Handler 进程。
                # 只要 stream_to_pusher 中的 taskkill 逻辑能确保 Handler 退出，这里就没有孤儿进程问题。
                continue # 跳到循环开始，再次检查播放列表/垫片
            # **********************************************

            try:
                # --- 夜间模式逻辑 ---
                if (time.localtime()[3] <= 5) and nightvideo:
                    print('night is comming~')
                    # ... (夜间模式文件查找和选择逻辑不变) ...
                    night_dir = os.path.join(path, 'resource', 'night')
                    night_files = os.listdir(night_dir)
                    if not night_files:
                        print("夜间文件夹为空，跳过")
                        time.sleep(60)
                        continue
                    
                    # (文件选择逻辑...)
                    night_files.sort()
                    night_ran = random.randint(0, len(night_files) - 1)
                    selected_file = night_files[night_ran]
                    full_file_path = os.path.join(night_dir, selected_file)

                    if selected_file.endswith(AUDIO_EXTENSIONS):
                        pic_dir = os.path.join(path, 'resource', 'img')
                        pic_files = os.listdir(pic_dir)
                        pic_files.sort()
                        pic_ran = random.randint(0, len(pic_files) - 1)
                        pic_path = os.path.join(pic_dir, pic_files[pic_ran])
                        seconds = get_audio_length(full_file_path)
                        print('audio long:' + convert_time(seconds))
                        
                        base_name = os.path.splitext(selected_file)[0]
                        ass_path = os.path.join(night_dir, base_name + '.ass')
                        
                        if not os.path.isfile(ass_path):
                            service.AssMaker.make_ass(os.path.join(path, 'night', base_name), '当前是晚间专属时间哦~时间范围：凌晨0-5点\\N大家晚安哦~做个好梦~\\N当前文件名：' + selected_file, path)
                        
                        ass_path_ffmpeg = ass_path.replace("\\", "/")
                        # 为 FFmpeg ass 过滤器转义冒号
                        ass_path_escaped = ass_path_ffmpeg.replace(":", "\\:")
                        ffmpeg_cmd = (
                            f"ffmpeg -threads 0 -loop 1 -r 2 -t {int(seconds)} -f image2 -i \"{pic_path}\" " 
                            f"-i \"{full_file_path}\" -vf \"ass='{ass_path_ffmpeg}'\" "
                            f"-pix_fmt yuv420p -b:v {config['rtmp']['bitrate']}k -g 10 "
                            f"-vcodec h264_qsv -af aformat=sample_rates=48000 -acodec aac -b:a 320k -f mpegts -"
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)  # 切歌后延迟，让 Pusher 完全处理完数据
                    continue 

                # --- 播放列表逻辑 ---
                playlist_dir = os.path.join(path, 'resource', 'playlist')
                
                while True: # 持续检查和播放播放列表中的文件
                    files = os.listdir(playlist_dir)
                    files.sort()
                    count = 0 # 用于标记是否找到了音频/视频文件
                    selected_file_to_play = None
                    
                    # 遍历查找一个文件来播放
                    for f in files:
                        full_file_path = os.path.join(playlist_dir, f)
                        
                        # --- 播放列表音频 (只播放第一个找到的合格文件) ---
                        if f.endswith(AUDIO_EXTENSIONS) and (f.find('.download') == -1):
                            # 秒数/码率检查逻辑
                            try:
                                seconds = get_audio_length(full_file_path)
                                if seconds == 0:
                                    print('无法获取音频长度，跳过该文件')
                                    continue
                            except Exception as e:
                                print(e)
                                continue
                            
                            selected_file_to_play = f
                            count = 1 # 标记找到了一个音频文件
                            break # 找到文件后，跳出 for 循环，准备播放
                        
                        # --- 播放列表视频 (FLV) ---
                        if (f.find('ok.flv') != -1) and (f.find('.download') == -1) and (f.find('rendering') == -1):
                            selected_file_to_play = f
                            count = 2 # 标记找到了一个视频文件
                            break # 找到文件后，跳出 for 循环，准备播放

                    # 如果没有找到任何可播放的媒体文件，跳出内部循环，进入垫片逻辑
                    if count == 0:
                        break # 跳出 while True 内部循环，进入垫片逻辑

                    # --- 播放逻辑 ---
                    f = selected_file_to_play
                    full_file_path = os.path.join(playlist_dir, f)

                    if count == 1: # 音频文件  
                        base_name = os.path.splitext(f)[0]
                        ass_path = os.path.join(playlist_dir, base_name + '.ass')
                        cover_path = os.path.join(playlist_dir, base_name + '.jpg')
                        # 构建 ASS 文件路径，转换为 ffmpeg 可识别的格式
                        ass_path_ffmpeg = ass_path.replace("\\", "/")
                        # 为 FFmpeg ass 过滤器转义冒号
                        ass_path_escaped = ass_path_ffmpeg.replace(":", "\\:")
                        ffmpeg_cmd = (
                            f"ffmpeg -threads 0 -loop 1 -re -r 2 -t {int(seconds)} -f image2 -i \"{cover_path}\" " 
                            f"-i \"{full_file_path}\" -vf \"ass='{ass_path_escaped}'\" "
                            f"-pix_fmt yuv420p -b:v {config['rtmp']['bitrate']}k -g 10 "
                            f"-bsf:v h264_mp4toannexb "
                            f"-vcodec h264_qsv -af aformat=sample_rates=48000 -acodec aac -b:a 320k -f mpegts -"
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)  # 切歌后延迟
                        # ****** 播放完后执行删除 ******
                        try:
                            # 删除 .info 和 .ass 和 .jpg文件
                            base_name = os.path.splitext(f)[0]
                            os.remove(os.path.join(playlist_dir, base_name + '.info'))
                            if os.path.exists(ass_path): os.remove(ass_path)
                            if os.path.exists(cover_path): os.remove(cover_path)
                            # 删除音频文件本身
                            if os.path.exists(full_file_path): os.remove(full_file_path)
                            print(f"成功删除播放列表音频文件: {f}")
                        except Exception as e:
                            print(f'delete error after playing: {e}')

                    elif count == 2: # 视频文件 (FLV)
                        print('flv:' + f)
                        
                        ffmpeg_cmd = (
                            f'ffmpeg -threads 1 -i "{full_file_path}" ' 
                            f'-af aformat=sample_rates=48000 -vcodec copy -acodec aac -b:a 320k -f flv -'
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)  # 切歌后延迟
                        
                        new_name = f.replace("ok", "")
                        os.rename(full_file_path, os.path.join(playlist_dir, new_name))
                        _thread.start_new_thread(remove_v, (new_name,))
                
                # --- 垫片音乐逻辑 (count == 0) ---
                if count == 0:
                    print('no media')
                    music_dir = os.path.join(path, 'resource', 'music')
                    mp3_files = os.listdir(music_dir)
                    if not mp3_files:
                        print("音乐文件夹为空，等待")
                        time.sleep(60)
                        continue
                        
                    # (文件选择逻辑...)
                    mp3_files.sort()
                    mp3_ran = random.randint(0, len(mp3_files) - 1)
                    selected_file = mp3_files[mp3_ran]
                    full_file_path = os.path.join(music_dir, selected_file)

                    # --- 垫片音频 ---
                    if selected_file.endswith(AUDIO_EXTENSIONS):
                        # ... (文件路径和 ASS 逻辑不变) ...
                        pic_dir = os.path.join(path, 'resource', 'img')
                        pic_files = os.listdir(pic_dir)
                        pic_files.sort()
                        pic_ran = random.randint(0, len(pic_files) - 1)
                        pic_path = os.path.join(pic_dir, pic_files[pic_ran])
                        
                        seconds = get_audio_length(full_file_path)
                        title = get_audio_title(full_file_path)
                        print(f'mp3 title: {title} long:{int(seconds)}')
                        
                        base_name = os.path.splitext(selected_file)[0]
                        ass_path = os.path.join(music_dir, base_name + '.ass')
                        jpg_path = os.path.join(music_dir, base_name + '.jpg')
                        
                        ffmpeg_cmd = ""
                        
                        # (有 ASS/JPG 的复杂逻辑)
                        if os.path.isfile(ass_path):
                            ass_path_ffmpeg = ass_path.replace("\\", "/")
                            ass_path_escaped = ass_path_ffmpeg.replace(":", "\\:")
                            if os.path.isfile(jpg_path):
                                # ass_filter_arg = f"filename='{ass_path}'"
                                ffmpeg_cmd = (
                                    f"ffmpeg -threads 0 -loop 1 -re -r 2 -t {int(seconds)} " 
                                    f"-f image2 -i \"{pic_path}\" -i \"{jpg_path}\" "
                                    f"-filter_complex \"[0:v][1:v]overlay=30:390[cover];[cover]ass='{ass_path_escaped}'\" "
                                    f"-i \"{full_file_path}\" -map \"[cover]\" -map 2:a " 
                                    f"-pix_fmt yuv420p -preset fast -maxrate {config['rtmp']['bitrate']}k -g 10 "
                                    f"-bsf:v h264_mp4toannexb "
                                    f"-af aformat=sample_rates=48000 -acodec aac -b:a 320k -c:v h264_qsv -f mpegts -"
                                )
                                stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                                time.sleep(0.2)  # 切歌后延迟
                            else:
                                ffmpeg_cmd = (
                                    f"ffmpeg -threads 0 -loop 1 -re -r 2 -t {int(seconds)} " 
                                    f"-f image2 -i \"{pic_path}\" -i \"{full_file_path}\" "
                                    f"-vf \"ass='{ass_path_escaped}'\" "
                                    f"-pix_fmt yuv420p -b:v {config['rtmp']['bitrate']}k -preset fast -g 10 "
                                    f"-bsf:v h264_mp4toannexb "
                                    f"-af aformat=sample_rates=48000 -acodec aac -b:a 320k -c:v h264_qsv -f mpegts -"
                                )
                                stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                                time.sleep(0.2)  # 切歌后延迟
                        # (没有 ASS 的简单逻辑)
                        else:
                            modify_ass_by_title(temp_ass_path, title)
                            temp_ass_path_ffmpeg = temp_ass_path.replace("\\", "/")
                            temp_ass_path_escaped = temp_ass_path_ffmpeg.replace(":", "\\:")
                            
                            ffmpeg_cmd = (
                                f"ffmpeg -threads 0 -re -loop 1 -r 2 -t {int(seconds)} " 
                                f"-f image2 -i \"{pic_path}\" -i \"{full_file_path}\" "
                                f"-vf \"ass='{temp_ass_path_escaped}'\" " 
                                f"-pix_fmt yuv420p -c:v h264_qsv -maxrate {config['rtmp']['bitrate']}k -preset fast -g 10 "
                                f"-af aformat=sample_rates=48000 -c:a aac -b:a 320k "
                                f"-bsf:v h264_mp4toannexb "
                                f"-f mpegts -" # 输出到标准输出，格式为 flv
                            )
                            stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                            time.sleep(0.2)  # 切歌后延迟

                    # --- 垫片视频 (FLV) ---
                    if selected_file.find('.flv') != -1:
                        ffmpeg_cmd = (
                            f'ffmpeg -threads 0 -i "{full_file_path}" ' 
                            f'-af aformat=sample_rates=48000 -vcodec copy -acodec aac -b:a 320k -f flv -'
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin, skip_flag_file)
                        time.sleep(0.2)  # 切歌后延迟 

            except BrokenPipeError:
                print("主循环检测到管道破坏！推流器已退出。正在尝试重新启动...")
                # 清理旧的进程对象
                if pusher_process and pusher_process.poll() is None:
                    pusher_process.kill()
                
                # 尝试重新启动推流器
                time.sleep(2)
                pusher_process = start_pusher(rtmp_url)
                pusher_stdin = pusher_process.stdin
                print("推流器已重新启动，继续主循环。")
            except Exception as e:
                print(f"主循环发生错误: {e}")
                print("5秒后重试...")
                time.sleep(5) 

    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C。正在关闭...")
    except Exception as e:
        print(f"致命错误：推流器无法启动或主循环崩溃：{e}")
    finally:
        print("清理...")
        if pusher_stdin:
            try:
                pusher_stdin.close()
            except Exception:
                pass
        if pusher_process and pusher_process.poll() is None:
            pusher_process.kill()
        print("所有进程已关闭。")


if __name__ == "__main__":
    # 确保路径是绝对路径
    path = os.path.abspath(path)
    print(f"使用的工作路径: {path}")
    
    # 临时 ASS 文件路径改为工作路径下，以确保跨平台兼容性
    # (Linux 的 /tmp 路径对 Windows 不友好)
    if os.name == 'nt': # Windows
        TEMP_ASS_PATH = os.path.join(path, 'temp.ass')
    else: # Linux/macOS
        TEMP_ASS_PATH = "/tmp/temp.ass" 
        
    main()