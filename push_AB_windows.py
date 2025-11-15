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

# --- 全局配置 ---
try:
    config = json.load(open('Config.json', encoding='utf-8'))
    path = config['path']
    rtmp = config['rtmp']['url']
    live_code = config['rtmp']['code']
    temp_ass_path = os.path.join("R:\\temp\\", 'temp.ass') # 使用工作路径下的临时文件
    nightvideo = bool(int(config['nightvideo']['use']))
    rtmp_url = rtmp + live_code
    # rtmp_url = "rtmp://192.168.3.249:1935/livehime"
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
    cmd_string = (
        f'ffmpeg -f flv -f mpegts -i - -c:a copy -c:v copy -f flv "{clean_rtmp_url}"'
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
    
def stream_to_pusher(ffmpeg_command, pusher_stdin):
    """
    进程 2：处理器。
    执行 ffmpeg 命令，捕获其 stdout，并将其写入推流器的标准输入 (stdin)。
    """
    print(f"--- 启动处理器 (进程 2) ---\n{ffmpeg_command}\n")
    process = None
    try:
        # 保持 shell=True 和 stderr=subprocess.PIPE
        process = subprocess.Popen(ffmpeg_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # 关键修改：立即启动 stderr 监控线程
        _thread.start_new_thread(monitor_handler_stderr, (process,))
        
        print("开始将数据流式传输到 Pusher...")
        
        while True:
            # 64k 缓冲区，用于读取 Handler 的输出
            data = process.stdout.read(65536) 
            if not data:
                break
            try:
                # 关键修改：将 Handler 的输出写入 Pusher 的 stdin
                pusher_stdin.write(data)
                # pusher_stdin.flush() # 在许多平台上，写入 PIPE 不需或不应频繁 flush
            except BrokenPipeError:
                print("错误：管道已损坏。推流器 (进程 1) 可能已崩溃。")
                if process.poll() is None: # 确保 Handler 进程还在运行
                    try:
                        print(f"尝试使用 taskkill 强制终止 Handler 进程树 (PID: {process.pid})...")
                        # /T 终止指定的进程及其由它启动的子进程；/F 强制终止
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        print("Handler 进程树终止成功。")
                    except subprocess.CalledProcessError as taskkill_err:
                        # taskkill 如果进程不存在会报错，可以忽略
                        print(f"Taskkill 终止失败 (可能已终止): {taskkill_err}")
                    except FileNotFoundError:
                        print("Taskkill 命令未找到，无法终止进程树。")
                # **********************************************
                
                raise # 重新抛出异常，让主循环处理 Pusher 重启逻辑
        
        process.wait()
        stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
        
        print(f"--- 处理器 (进程 2) 已完成 (RC: {process.returncode}) ---")
        
        if process.returncode != 0:
            print("FFmpeg (进程 2) 错误输出:")
            print(stderr_output)
            # 如果 Handler 失败，强制退出以重新启动整个流程
            if process.returncode != 255: # 255 通常是用户中止，可以忽略
                raise Exception("Handler 进程执行失败")

    except Exception as e:
        print(f"stream_to_pusher 发生严重错误: {e}")
        if process and process.poll() is None:
            process.kill()
        raise

def main():
    # 1. 启动推流器 (进程 1) 并获取其 stdin
    pusher_process = None
    pusher_stdin = None

    try:
        pusher_process = start_pusher(rtmp_url)
        pusher_stdin = pusher_process.stdin
        print("推流器已启动。主循环开始。")
            
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
                        
                        audio = mutagen.File(full_file_path)
                        seconds = audio.info.length
                        print('mp3 long:' + convert_time(seconds))
                        
                        base_name = os.path.splitext(selected_file)[0]
                        ass_path = os.path.join(night_dir, base_name + '.ass')
                        
                        if not os.path.isfile(ass_path):
                            service.AssMaker.make_ass(os.path.join(path, 'night', base_name), '当前是晚间专属时间哦~时间范围：凌晨0-5点\\N大家晚安哦~做个好梦~\\N当前文件名：' + selected_file, path)
                        
                        ffmpeg_cmd = (
                            f'ffmpeg -threads 0 -loop 1 -r 2 -t {int(seconds)} -f image2 -i "{pic_path}" ' 
                            f'-i "{full_file_path}" -vf ass=filename="{convert_ass_path_format(ass_path)}" '
                            f'-pix_fmt yuv420p -b:v {config["rtmp"]["bitrate"]}k -g 10 '
                            f'-vcodec h264_qsv -acodec aac -b 320k -f mpegts -' # 输出到标准输出，格式为 flv
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin) # 传入 pusher_stdin
                    continue 
# --- 播放列表逻辑 ---
                playlist_dir = os.path.join(path, 'resource', 'playlist')
                
                # ****** 核心修改开始：增加一个内部循环来持续处理播放列表 ******
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
                            # ... (秒数/码率检查逻辑不变) ...
                            try:
                                audio = mutagen.File(full_file_path)
                                seconds = audio.info.length
                                bitrate = audio.info.bitrate
                            except Exception as e:
                                print(e)
                                bitrate = 99999999999                           
                            if (seconds > 600) or (bitrate > 400000):
                                print('too long/too big, delete')
                                try:
                                    base_name_to_delete = os.path.splitext(f)[0]
                                    os.remove(os.path.join(playlist_dir, base_name_to_delete + '.info'))
                                    if os.path.exists(full_file_path): os.remove(full_file_path)
                                    ass_path_to_delete = os.path.join(playlist_dir, base_name_to_delete + '.ass')
                                    if os.path.exists(ass_path_to_delete): os.remove(ass_path_to_delete)
                                except Exception as e:
                                    print(f'delete error: {e}')
                                continue # 检查下一个文件
                            
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
                        # ... (播放列表音频的 FFmpeg 命令构造和 stream_to_pusher 逻辑) ...
                        
                        pic_dir = os.path.join(path, 'resource', 'img')
                        pic_files = os.listdir(pic_dir)
                        pic_files.sort()
                        pic_ran = random.randint(0, len(pic_files) - 1)
                        pic_path = os.path.join(pic_dir, pic_files[pic_ran])
                        
                        base_name = os.path.splitext(f)[0]
                        ass_path = os.path.join(playlist_dir, base_name + '.ass')

                        ffmpeg_cmd = (
                            f'ffmpeg -threads 0 -loop 1 -re -r 2 -t {int(seconds)} -f image2 -i "{pic_path}" ' 
                            f'-i "{full_file_path}" -vf ass=filename="{(ass_path).replace("\\","/").replace(":/","\\\\:/")}" '
                            f'-pix_fmt yuv420p -b:v {config["rtmp"]["bitrate"]}k -g 4 '
                            f'-bsf:v h264_mp4toannexb '
                            f'-vcodec h264_qsv -acodec aac -b:a 320k -f mpegts -'
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin) 
                        # ****** 播放完后执行删除 ******
                        try:
                            # 删除 .info 和 .ass 文件
                            base_name = os.path.splitext(f)[0]
                            os.remove(os.path.join(playlist_dir, base_name + '.info'))
                            if os.path.exists(ass_path): os.remove(ass_path)
                            # 删除音频文件本身
                            if os.path.exists(full_file_path): os.remove(full_file_path)
                            print(f"成功删除播放列表音频文件: {f}")
                        except Exception as e:
                            print(f'delete error after playing: {e}')

                    elif count == 2: # 视频文件 (FLV)
                        print('flv:' + f)
                        
                        ffmpeg_cmd = (
                            f'ffmpeg -threads 1 -i "{full_file_path}" ' 
                            f'-vcodec copy -acodec copy -f flv -'
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin) 
                        
                        # 视频文件的处理逻辑保持不变 (重命名后在单独线程中删除)
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
                        
                        audio = mutagen.File(full_file_path)
                        seconds = audio.info.length
                        title = get_audio_title(full_file_path)
                        print(f'mp3 title: {title} long:{int(seconds)}')
                        
                        base_name = os.path.splitext(selected_file)[0]
                        ass_path = os.path.join(music_dir, base_name + '.ass')
                        jpg_path = os.path.join(music_dir, base_name + '.jpg')
                        
                        ffmpeg_cmd = ""
                        
                        # (有 ASS/JPG 的复杂逻辑)
                        if os.path.isfile(ass_path):
                            if os.path.isfile(jpg_path):
                                # ass_filter_arg = f"filename='{ass_path}'"
                                ffmpeg_cmd = (
                                    f'ffmpeg -threads 0 -loop 1 -re -r 2 -t {int(seconds)} ' 
                                    f'-f image2 -i "{pic_path}" -i "{jpg_path}" '
                                    f'-filter_complex "[0:v][1:v]overlay=30:390[cover];[cover]ass=filename="{(ass_path).replace("\\","/").replace(":/","\\\\:/")}" '
                                    f'-i "{full_file_path}" -map "[result]" -map 2:a ' 
                                    f'-pix_fmt yuv420p -preset fast -maxrate {config["rtmp"]["bitrate"]}k -g 4 '
                                    f'-bsf:v h264_mp4toannexb '
                                    f'-acodec aac -b:a 320k -c:v h264_qsv -f mpegts -'
                                )
                                stream_to_pusher(ffmpeg_cmd, pusher_stdin) # 传入 pusher_stdin
                            else:
                                ffmpeg_cmd = (
                                    f'ffmpeg -threads 0 -loop 1 -re -r 2 -t {int(seconds)} ' 
                                    f'-f image2 -i "{pic_path}" -i "{full_file_path}" '
                                    f'-vf ass=filename="{(ass_path).replace("\\","/").replace(":/","\\\\:/")}" '
                                    f'-b:v {config["rtmp"]["bitrate"]}k -g 4 '
                                    f'-pix_fmt yuv420p -preset fast '
                                    f'-bsf:v h264_mp4toannexb '
                                    f'-maxrate {config["rtmp"]["bitrate"]}k -acodec aac -b:a 320k -c:v h264_qsv -f mpegts -'
                                )
                                stream_to_pusher(ffmpeg_cmd, pusher_stdin) # 传入 pusher_stdin
                        # (没有 ASS 的简单逻辑)
                        else:
                            modify_ass_by_title(temp_ass_path, title)
                            
                            ffmpeg_cmd = (
                                f'ffmpeg -threads 0 -re -loop 1 -r 2 -t {int(seconds)} ' 
                                f'-f image2 -i "{pic_path}" -i "{full_file_path}" '
                                f'-vf ass=filename="{convert_ass_path_format(temp_ass_path)}" '
                                f'-c:v h264_qsv -maxrate {config["rtmp"]["bitrate"]}k -pix_fmt yuv420p -preset fast -g 4 '
                                f'-c:a aac -b:a 320k '
                                f'-bsf:v h264_mp4toannexb '
                                f'-f mpegts -' # 输出到标准输出，格式为 flv
                            )
                            # 注意：此处的 FFmpeg 命令结构似乎有点奇怪，但保留了其核心逻辑
                            stream_to_pusher(ffmpeg_cmd, pusher_stdin) # 传入 pusher_stdin

                    # --- 垫片视频 (FLV) ---
                    if selected_file.find('.flv') != -1:
                        ffmpeg_cmd = (
                            f'ffmpeg -threads 0 -i "{full_file_path}" ' 
                            f'-vcodec copy -acodec copy -f flv -' # 输出到标准输出，格式为 flv
                        )
                        stream_to_pusher(ffmpeg_cmd, pusher_stdin) # 传入 pusher_stdin

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