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
import subprocess # 用于更高级的进程管理
import os.path   # 用于跨平台的路径处理

# --- 全局配置 ---

try:
    config = json.load(open('Config.json', encoding='utf-8'))
    path = config['path']
    rtmp = config['rtmp']['url']
    live_code = config['rtmp']['code']
    nightvideo = bool(int(config['nightvideo']['use']))
    rtmp_url = rtmp + live_code
    # rtmp_url = "rtmp://192.168.31.217:1935/livehime"
except FileNotFoundError:
    print("错误：Config.json 未找到。请确保配置文件存在。")
    sys.exit(1)
except KeyError as e:
    print(f"错误：Config.json 缺少键 {e}。")
    sys.exit(1)


# Linux 上的 FIFO 路径
FIFO_PATH = "/tmp/live_stream.fifo"

# 支持的音频格式
AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac')

# --- 辅助函数 (保持不变) ---

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
    修改 ASS 文件
    """
    try:
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

# --- 核心推流函数 (进程 1 和 2) ---

def start_pusher(fifo_path, rtmp_url):
    """
    进程 1：推流器。
    在一个单独的线程中运行，从 FIFO 读取并推流到 RTMP。
    """
    # local_udp_url = 'udp://127.0.0.1:1234' # 调试模式的地址
    local_rtmp_url = 'rtmp://127.0.0.1:1935/live/teststream' #virtual camera
    cmd = [
        'ffmpeg',
        '-re',           # 以本机帧率读取
        '-i', fifo_path, # 从 FIFO 读取
        '-r','30 '
        '-pix_fmt yuv420p '
        '-c:a copy '
        '-c:v libx264 '
        '-bufsize 320k '
        '-g 60 -crf 33 '
        '-f', 'flv',
        rtmp_url
        # local_rtmp_url
        # local_udp_url
    ]
    print(f"--- 启动推流器 (进程 1) ---\n{' '.join(cmd)}\n")
    process = subprocess.Popen(' '.join(cmd), shell=True, stderr=subprocess.PIPE)
    
    # 监控推流器的 stderr
    while True:
        line = process.stderr.readline().decode('utf-8', errors='ignore')
        if not line and process.poll() is not None:
            break
        if line.strip():
            print(f"[Pusher]: {line.strip()}")
            
    print("--- 推流器 (进程 1) 已退出 ---")

def stream_to_fifo(ffmpeg_command, fifo_stream):
    """
    进程 2：处理器。
    执行 ffmpeg 命令，捕获其 stdout，并将其写入 FIFO 流。
    """
    print(f"--- 启动处理器 (进程 2) ---\n{ffmpeg_command}\n")
    
    try:
        process = subprocess.Popen(ffmpeg_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        print("开始将数据流式传输到 FIFO...")
        
        while True:
            data = process.stdout.read(65536) # 64k 缓冲区
            if not data:
                break
            try:
                fifo_stream.write(data)
                fifo_stream.flush() 
            except BrokenPipeError:
                print("错误：管道已损坏。推流器 (进程 1) 可能已崩溃。")
                process.kill()
                raise 
        
        process.wait()
        stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
        
        print(f"--- 处理器 (进程 2) 已完成 (RC: {process.returncode}) ---")
        
        if process.returncode != 0:
            print("FFmpeg (进程 2) 错误输出:")
            print(stderr_output)

    except Exception as e:
        print(f"stream_to_fifo 发生严重错误: {e}")
        if 'process' in locals() and process.poll() is None:
            process.kill()
        raise

# --- 主逻辑 ---

def main():
    # 1. 创建 FIFO
    if not os.path.exists(FIFO_PATH):
        try:
            os.mkfifo(FIFO_PATH)
            print(f"已创建 FIFO: {FIFO_PATH}")
        except Exception as e:
            print(f"创建 FIFO 失败: {e}")
            sys.exit(1)
    else:
        print(f"FIFO 已存在: {FIFO_PATH}")

    # 2. 在新线程中启动推流器 (进程 1)
    _thread.start_new_thread(start_pusher, (FIFO_PATH, rtmp_url))

    print("正在等待推流器连接到 FIFO... (5s)")
    time.sleep(5) 

    # 3. 打开 FIFO 的写入端
    try:
        with open(FIFO_PATH, 'wb') as fifo_write_stream:
            print("FIFO 写入端已打开。主循环开始。")
            
            # 4. 启动主循环 (进程 2 的管理器)
            while True:
                try:
                    # --- 夜间模式逻辑 ---
                    if (time.localtime()[3] <= 5) and nightvideo:
                        print('night is comming~')
                        night_dir = os.path.join(path, 'resource', 'night')
                        night_files = os.listdir(night_dir)
                        if not night_files:
                            print("夜间文件夹为空，跳过")
                            time.sleep(60)
                            continue
                        
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
                                f'ffmpeg -threads 1 -loop 1 -r 30 -t {int(seconds)} -f image2 -i "{pic_path}" ' # <-- 修正 2: 移除 -re, 帧率提高到 30
                                f'-i "{full_file_path}" -vf ass=filename="{ass_path}" '
                                f'-x264-params "profile=high:level=5.1" -pix_fmt yuv420p -b:v {config["rtmp"]["bitrate"]}k '
                                f'-vcodec libx264 -acodec copy -f flv -'
                            )
                            stream_to_fifo(ffmpeg_cmd, fifo_write_stream)
                        continue 

                    # --- 播放列表逻辑 ---
                    playlist_dir = os.path.join(path, 'resource', 'playlist')
                    files = os.listdir(playlist_dir)
                    files.sort()
                    count = 0
                    
                    for f in files:
                        full_file_path = os.path.join(playlist_dir, f)
                        
                        # --- 播放列表音频 ---
                        if f.endswith(AUDIO_EXTENSIONS) and (f.find('.download') == -1):
                            print(full_file_path)
                            seconds = 600
                            bitrate = 0
                            try:
                                audio = mutagen.File(full_file_path)
                                seconds = audio.info.length
                                bitrate = audio.info.bitrate
                            except Exception as e:
                                print(e)
                                bitrate = 99999999999
                            
                            print('mp3 long:' + convert_time(seconds))
                            if (seconds > 600) or (bitrate > 400000):
                                print('too long/too big,delete')
                                # ... (删除逻辑) ...
                            else:
                                pic_dir = os.path.join(path, 'resource', 'img')
                                pic_files = os.listdir(pic_dir)
                                pic_files.sort()
                                pic_ran = random.randint(0, len(pic_files) - 1)
                                pic_path = os.path.join(pic_dir, pic_files[pic_ran])
                                
                                base_name = os.path.splitext(f)[0]
                                ass_path = os.path.join(playlist_dir, base_name + '.ass')

                                ffmpeg_cmd = (
                                    f'ffmpeg -threads 1 -loop 1 -r 30 -t {int(seconds)} -f image2 -i "{pic_path}" ' # <-- 修正 2: 移除 -re, 帧率提高到 30
                                    f'-i "{full_file_path}" -vf ass=filename="{ass_path}" '
                                    f'-x264-params "profile=high:level=5.1" -pix_fmt yuv420p -b:v {config["rtmp"]["bitrate"]}k '
                                    f'-vcodec libx264 -acodec copy -f flv -'
                                )
                                stream_to_fifo(ffmpeg_cmd, fifo_write_stream)
                                
                                try:
                                    shutil.move(full_file_path, os.path.join(path, 'resource', 'music/'))
                                    shutil.move(ass_path, os.path.join(path, 'resource', 'music/'))
                                except Exception as e:
                                    print(e)
                            
                            try:
                                base_name = os.path.splitext(f)[0]
                                os.remove(os.path.join(playlist_dir, base_name + '.info'))
                                if os.path.exists(full_file_path): os.remove(full_file_path)
                                if os.path.exists(ass_path): os.remove(ass_path)
                            except Exception as e:
                                print(f'delete error: {e}')
                            
                            count += 1
                            break 

                        # --- 播放列表视频 (FLV) ---
                        if (f.find('ok.flv') != -1) and (f.find('.download') == -1) and (f.find('rendering') == -1):
                            print('flv:' + f)
                            
                            ffmpeg_cmd = (
                                f'ffmpeg -threads 1 -i "{full_file_path}" ' # <-- 修正 2: 移除 -re
                                f'-vcodec copy -acodec copy -f flv -'
                            )
                            stream_to_fifo(ffmpeg_cmd, fifo_write_stream)
                            
                            new_name = f.replace("ok", "")
                            os.rename(full_file_path, os.path.join(playlist_dir, new_name))
                            _thread.start_new_thread(remove_v, (new_name,))
                            count += 1
                            break 
                    
                    # --- 垫片音乐逻辑 (count == 0) ---
                    if count == 0:
                        print('no media')
                        music_dir = os.path.join(path, 'resource', 'music')
                        mp3_files = os.listdir(music_dir)
                        if not mp3_files:
                            print("音乐文件夹为空，等待")
                            time.sleep(60)
                            continue
                            
                        mp3_files.sort()
                        mp3_ran = random.randint(0, len(mp3_files) - 1)
                        selected_file = mp3_files[mp3_ran]
                        full_file_path = os.path.join(music_dir, selected_file)

                        # --- 垫片音频 ---
                        if selected_file.endswith(AUDIO_EXTENSIONS):
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
                            
                            if os.path.isfile(ass_path):
                                if os.path.isfile(jpg_path):
                                    ass_filter_arg = f"filename='{ass_path}'"
                                    ffmpeg_cmd = (
                                        f'ffmpeg -threads 0 -loop 1 -r 24 -t {int(seconds)} ' # <-- 修正 2: 移除 -re
                                        f'-f image2 -i "{pic_path}" -i "{jpg_path}" '
                                        f'-filter_complex "[0:v][1:v]overlay=30:390[cover];[cover]ass={ass_filter_arg}[result]" '
                                        f'-i "{full_file_path}" -map "[result]" -map 2:a ' 
                                        f'-pix_fmt yuv420p -preset ultrafast -maxrate {config["rtmp"]["bitrate"]}k '
                                        f'-acodec copy -c:v libx264 -f flv -'
                                    )
                                else:
                                    ffmpeg_cmd = (
                                        f'ffmpeg -threads 0 -loop 1 -r 30 -t {int(seconds)} ' # <-- 修正 2: 移除 -re, 帧率提高到 30
                                        f'-f image2 -i "{pic_path}" -i "{full_file_path}" '
                                        f'-vf ass=filename="{ass_path}" -pix_fmt yuv420p -preset ultrafast '
                                        f'-maxrate {config["rtmp"]["bitrate"]}k -acodec copy -c:v libx264 -f flv -'
                                    )
                            else:
                                # 适配：使用 /tmp/temp.ass
                                temp_ass_path = "/tmp/temp.ass"
                                modify_ass_by_title(temp_ass_path, title)
                                
                                ffmpeg_cmd = (
                                    f'ffmpeg -threads 0 -loop 1 -r 30 -t {int(seconds)} ' 
                                    f'-f image2 -i "{pic_path}" -i "{full_file_path}" '
                                    f'-vf ass=filename="{temp_ass_path}" '
                                    # f'-bufsize 320k -c:v libx264 -g 30 -crf 33 -f flv -'
                                    f'-c:v libx264 -maxrate {config["rtmp"]["bitrate"]}k -pix_fmt yuv420p -preset fast '
                                    f'-c:a aac -b:a 320k '
                                    f'-f mpegts -'
                                    # f'ffmpeg -i ~/test.flv -pix_fmt yuv420p -c:a copy -c:v copy -f flv -'
                                )
                            
                            stream_to_fifo(ffmpeg_cmd, fifo_write_stream)

                        # --- 垫片视频 (FLV) ---
                        if selected_file.find('.flv') != -1:
                            ffmpeg_cmd = (
                                f'ffmpeg -threads 0 -i "{full_file_path}" ' # <-- 修正 2: 移除 -re
                                f'-vcodec copy -acodec copy -f flv -'
                            )
                            stream_to_fifo(ffmpeg_cmd, fifo_write_stream)

                except BrokenPipeError:
                    print("主循环检测到管道破坏！推流器已退出。正在尝试退出...")
                    break 
                except Exception as e:
                    print(f"主循环发生错误: {e}")
                    print("5秒后重试...")
                    time.sleep(5) 

    except IOError as e:
        if e.errno == 32: # Broken pipe
             print("推流器在主循环开始前已断开。")
        else:
             print(f"无法打开 FIFO {FIFO_PATH} 进行写入: {e}")
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C。正在关闭...")
    finally:
        print("清理... (推流器线程将自动退出)")

if __name__ == "__main__":
    # 确保路径是绝对路径
    path = os.path.abspath(path)
    print(f"使用的工作路径: {path}")
    main()