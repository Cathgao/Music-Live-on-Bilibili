import asyncio
import json
from bilibili_api import Credential, Danmaku, sync
from bilibili_api.live import LiveDanmaku, LiveRoom
import os
import service.AssMaker
import time, datetime
import urllib
import urllib.request
import requests
from PIL import Image
import io

# --- 配置加载 ---
config = json.load(open('./Config.json', encoding='utf-8'))

credential = Credential(sessdata=config["danmu"]["SESSDATA"], bili_jct=config["danmu"]["bili_jct"], buvid3=config["danmu"]["buvid3"], ac_time_value=config["danmu"]["ac_time_value"])
monitor  = LiveDanmaku(int(config['danmu']['roomid']), credential=credential)
sender = LiveRoom(int(config['danmu']['roomid']), credential=credential)
path = config['path']
temp_path = "R:\\temp\\"
roomid = config['danmu']['roomid']
download_api_url = config['musicapi']
neteasemusic_api_url = "http://127.0.0.1:4055"
qqmusic_api_url = "http://127.0.0.1:4055"
# 切歌标记文件
skip_flag_file = os.path.join(temp_path, '.skip_current')

AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac')

dm_lock = False		 # 弹幕发送锁，用来排队
encode_lock = False	 # 视频渲染锁，用来排队
rp_lock = False      # 点播锁定开关
first_order = False    # 首次点歌标记

# --- 图片处理函数 ---
def resize_image_to_1080p(image_bytes):
    """
    将任意大小的图片处理成标准1080P (1920x1080)。
    - 过大的图片：保持长宽比缩小至填满至少一边，然后加黑边
    - 过小的图片：保持长宽比放大至填满至少一边，然后加黑边
    确保所有输出图片都是标准的 1920x1080 分辨率，无裁切。
    
    参数:
        image_bytes: 图片的字节流 (bytes)
    
    返回:
        处理后的图片字节流 (bytes)，如果处理失败返回 None
    """
    try:
        # 从字节流打开图片
        img = Image.open(io.BytesIO(image_bytes))
        
        # 确保是 RGB 格式（处理 RGBA 等格式）
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # 目标分辨率
        target_width = 1920
        target_height = 1080
        
        # 计算缩放比例，保持长宽比，取较大的比例（确保填满至少一边）
        img_ratio = img.width / img.height
        target_ratio = target_width / target_height
        
        if img_ratio > target_ratio:
            # 图片相对宽，按宽度缩放到 1920
            new_width = target_width
            new_height = int(target_width / img_ratio)
        else:
            # 图片相对高，按高度缩放到 1080
            new_height = target_height
            new_width = int(target_height * img_ratio)
        
        # 缩放图片（使用高质量重采样）
        img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # 创建 1920x1080 的黑色背景
        bg = Image.new('RGB', (target_width, target_height), color=(0, 0, 0))
        
        # 计算居中位置
        x = (target_width - new_width) // 2
        y = (target_height - new_height) // 2
        
        # 将缩放后的图片粘贴到背景中心
        bg.paste(img_resized, (x, y))
        
        # 保存处理后的图片到内存中
        output_bytes = io.BytesIO()
        bg.save(output_bytes, format='JPEG', quality=95)
        processed_image = output_bytes.getvalue()
        
        print(f"✅ 图片已在内存中处理完毕，缩放至 {new_width}x{new_height}，黑边填充至 1920x1080，大小: {len(processed_image)} bytes")
        return processed_image
        
    except Exception as e:
        print(f"❌ 图片处理出错: {e}")
        return None

@monitor.on('DANMU_MSG')
async def on_danmaku(event):
    # 收到弹幕
    print(event)
    if(event["data"]["cmd"]=="DANMU_MSG"):
        commentUser = event['data']['info'][2][1]
        commentText = event['data']['info'][1]
        commentUserID = event['data']['info'][2][0]
    print(f'{commentUser}({commentUserID})说: {commentText}')
    await danmuji.pick_msg(commentUser, commentUserID, commentText)

@monitor.on('SEND_GIFT')
async def on_gift(event):
    # 收到礼物
    print(event)

# --- 同步文件操作/空间检查（保持同步，但只在需要时调用） ---

def del_file(f):
    try:
        print('delete'+path+'/resource/playlist/'+f)
        os.remove(path+'/resource/playlist/'+f)
    except Exception as e:
        print(f'delete error: {e}')

def check_free():
    files = os.listdir(path+'/resource/playlist')
    size = 0
    for f in files:
        size += os.path.getsize(path+'/resource/playlist/'+f)
    files = os.listdir(path+'/resource/music')
    for f in files:
        size += os.path.getsize(path+'/resource/music/'+f)
    
    # 转换为兆字节并比较
    if(size > int(config['freespace'])*1024*1024):
        print(f"space size: {size} bytes, exceeded limit.")
        return True
    else:
        return False

# 检查已使用空间，并在超过时，自动删除缓存的视频
def clean_files():
    # 这里的逻辑与原始代码相似，但请注意：
    # 原始的 clean_files() 内部逻辑被注释掉了，这里只保留了检查并返回 True/False 的功能
    is_boom = check_free()
    if is_boom:
         # 这里应该添加实际的删除缓存文件的逻辑
         print("Warning: Storage space exceeded. Deletion logic missing or commented out.")
         pass
    return is_boom

# --- 核心异步函数：下载和渲染优化 ---

async def get_download_url(songid, type, user, userID, songname = "nothing"):
    global encode_lock
    
    # 检查空间（同步操作，但速度快，可直接调用）
    if clean_files():
        await danmuji.send_dm('Server存储空间已爆炸，请联系up')
        return

    # await danmuji.send_dm(f'正在下载 {type}{songid}')
    print(f'[log] getting url: {type}{songid}')
    filename = str(int(time.mktime(datetime.datetime.now().timetuple())))

    try:
        if type == 'id':
            
            # --- 同步下载函数定义 (使用 to_thread 运行) ---
            def sync_download_id():

                if(config["QQmusic"]["use"] == 1):
                    # QQ音乐的API
                    api_url = qqmusic_api_url + "/qq/song"
                    payload = {
                        "ids" : songid,
                    }
                    response = requests.get(api_url, params=payload)
                else:
                    # 网易云的API
                    api_url = neteasemusic_api_url + "/song"
                    payload = {
                        "ids" : songid,
                        "level" : "lossless",
                        "type" : "json"
                    }
                    response = requests.post(api_url, data=payload)
                
                

                if response.status_code == 200:
                    #获取歌曲信息
                    if(config["QQmusic"]["use"] == 1):
                        # QQ音乐API
                        song_data = response.json()
                        song_temp = song_data["song"]["name"]
                        pic_url = song_data["song"]["pic"]
                        # 会返回多个URL，音质最高的应该是最后一个
                        download_url = song_data["music_urls"][list(song_data["music_urls"].keys())[-1]]["url"]
                        lyric = song_data["lyric"]["lyric"]
                        tlyric = song_data["lyric"]["tylyric"]
                    else:
                        # 网易云API
                        song_data = response.json()
                        song_temp = song_data["name"]
                        pic_url = song_data["pic"]
                        download_url = song_data["url"]
                        lyric = song_data["lyric"]
                        tlyric = song_data["tlyric"]

                    #下载专辑封面
                    pic_response = requests.get(pic_url, stream=True, timeout=10)
                    if pic_response.status_code == 200:
                        try:
                            pic_bytes = pic_response.content
                            processed_pic_bytes = resize_image_to_1080p(pic_bytes)
                            if processed_pic_bytes:
                                final_pic_path = f'{path}/resource/playlist/{filename}.jpg'
                                with open(final_pic_path, 'wb') as f_pic:
                                    f_pic.write(processed_pic_bytes)
                                print(f"✅ 封面图片成功保存到: {final_pic_path}")
                            else:
                                print(f"❌ 图片处理失败，跳过保存")
                        finally:
                            # 显式释放内存
                            del pic_bytes
                            del processed_pic_bytes
                            pic_response.close()
                    
                    #下载歌曲
                    if(config["QQmusic"]["use"] == 1):
                        # QQ音乐
                        header = {
                            'Cookie':config["QQmusic"]["cookie"]
                        }
                        response = requests.get(download_url, stream=True, timeout=10,headers=header)
                    else:
                        # 网易云
                        response = requests.get(download_url, stream=True, timeout=10)
                    try:
                        if response.status_code == 200:
                            _, extension_name = os.path.splitext(os.path.basename(urllib.parse.urlparse(download_url).path).split('?')[0])
                            with open(f'{path}/resource/playlist/{filename}{extension_name}', 'wb') as f:
                                # 推荐使用 8KB (8192) 的数据块大小
                                for chunk in response.iter_content(chunk_size=8192):
                                    if chunk: # 过滤掉保持连接的空数据块
                                        f.write(chunk)                    
                            print(f"✅ 文件成功下载并保存到: {f'{path}/resource/playlist/{filename}{extension_name}'}")
                            return lyric, tlyric, song_temp
                        else:
                            print(f"❌ 无法获取歌曲信息，HTTP状态码: {response.status_code}")
                            return "", "", ""
                    finally:
                        response.close()
                else:
                    print(f"❌ 无法下载文件，HTTP状态码: {response.status_code}")
                    return "", "", ""
                
            # 使用 asyncio.to_thread 运行阻塞任务
            lyric, tlyric, song_temp = await asyncio.to_thread(sync_download_id)
            if(not song_temp):
                await danmuji.send_dm('点歌失败')
                return
            
            song = f"歌名：{song_temp}" if song_temp else f"关键词：{songname}"
            if(config["QQmusic"]["use"] == 1):
                service.AssMaker.make_ass(filename, f'当前QQ音乐id：{songid}\\N{song}\\N点播人：{user}', path, lyric, tlyric)
            else:
                service.AssMaker.make_ass(filename, f'当前网易云id：{songid}\\N{song}\\N点播人：{user}', path, lyric, tlyric)
            service.AssMaker.make_info(filename, f'id：{songid},{song},点播人：{user}', userID, path)
            # 第一首点播歌曲直接切
            global first_order
            if(first_order):
                first_order = False
                try:
                    with open(skip_flag_file, 'w') as f:
                        f.write('skip')
                    print(f'[log] 发送切歌信号')
                    await danmuji.send_dm(f'{type}{songid} 下载完成，准备播放')
                except Exception as e:
                    print(f'[log] 切歌信号发送失败: {e}')
            else:      
                await danmuji.send_dm(f'{type}{songid} 下载完成，已加入播放队列')
                print(f'[log] 已添加排队项目：{type}{songid}')

        elif type == 'mv':
            def sync_process_mv():
                # 1. 获取 MV URL
                params = urllib.parse.urlencode({type: songid})
                f = urllib.request.urlopen(download_api_url + "?%s" % params, timeout=5)
                url = f.read().decode('utf-8')
                
                # 2. 下载 MV
                urllib.request.urlretrieve(url, f'{path}/resource/playlist/{filename}.mp4')
                
                return url # 返回 URL 供日志使用

            url = await asyncio.to_thread(sync_process_mv) # 在线程池中执行
            
            print(f'[log] 获取{type}{songid}网址：{url}')
            print(f'[log] {type}{songid} 下载完成')
            
            # 生成字幕信息（非阻塞）
            info_text = f"当前MV网易云id：{songid}\\N" + \
                        (f"MV点播关键词：{songname}\\N" if songname != "nothing" else "") + \
                        f"点播人：{user}"
            service.AssMaker.make_ass(f'{filename}ok', info_text, path)
            service.AssMaker.make_info(f'{filename}ok', info_text.replace('\\N', ','), path)
            
            await danmuji.send_dm(f'{type}{songid} 下载完成，等待渲染')
            
            # 渲染锁（等待渲染完成）
            while encode_lock:
                await asyncio.sleep(1)
            encode_lock = True
            
            await danmuji.send_dm(f'{type}{songid} 正在渲染')
            print(f'[log] {type}{songid} 正在渲染')
            
            def sync_render_mv():
                cmd = f'ffmpeg -threads 1 -i "{path}/resource/playlist/{filename}.mp4" -aspect 16:9 -vf "scale=1280:720, ass={path}/resource/playlist/{filename}ok.ass" -c:v libx264 -strict -2 -preset ultrafast -maxrate {config["rtmp"]["bitrate"]}k -tune fastdecode -acodec aac -b:a 192k "{path}/resource/playlist/{filename}rendering.flv"'
                os.system(cmd)
            
            await asyncio.to_thread(sync_render_mv) # 在线程池中执行 FFMPEG
            
            encode_lock = False
            del_file(f'{filename}.mp4')
            os.rename(f'{path}/resource/playlist/{filename}rendering.flv', f'{path}/resource/playlist/{filename}ok.flv')
            
            await danmuji.send_dm(f'{type}{songid} 渲染完毕，已加入播放队列')
            print(f'[log] {type}{songid} 渲染完毕，已加入播放队列')

    except Exception as e:
        await danmuji.send_dm('出错了：请检查命令或重试')
        print(f'[log] 下载文件出错：{type}{songid}')
        print(e)
        del_file(f'{filename}.mp3')
        del_file(f'{filename}.mp4')
        del_file(f'{filename}.flv')

# 下载歌单
async def playlist_download(id,user):
    def sync_get_playlist():
        params = urllib.parse.urlencode({'playlist': str(id)})
        f = urllib.request.urlopen(download_api_url + "?%s" % params, timeout=3)
        return json.loads(f.read().decode('utf-8'))
    
    try:
        playlist = await asyncio.to_thread(sync_get_playlist) # 在线程池中获取
        await danmuji.send_dm(f'正在下载歌单：{playlist["playlist"]["name"]}，共{len(playlist["playlist"]["tracks"])}首')
    except Exception as e:
        print(f'shit(playlist): {e}')
        await danmuji.send_dm('出错了：请检查命令或重试')
        return

    # 遍历歌单歌曲并启动下载任务
    for song in playlist['playlist']['tracks']:
        print(f'name:{song["name"]} id:{song["id"]}')
        asyncio.create_task(song['id'], 'id', user, song['name'])

# 搜索歌曲并下载
async def search_song(song_name,user,userID):
    print(f'[log] searching song: {song_name}')
    def sync_search():
        payload = {
            "keywords" : song_name,
            "limit" : 1
        }
        # 判断使用QQ音乐还是网易云
        if(config["QQmusic"]["use"] == 1):
            url = qqmusic_api_url + "/qq/search"
            response = requests.get(url, params=payload)
        else:
            url = neteasemusic_api_url + "/search"
            response = requests.post(url, data=payload)
        if response.status_code == 200:
            search_result = response.json()
            return search_result
        else:
            return {"result": None}
    try:
        search_result = await asyncio.to_thread(sync_search) # 在线程池中获取搜索结果
        
        # 检查结果是否存在
        if not search_result["result"]:
             await danmuji.send_dm(f'未找到歌曲：{song_name}')
             return
             
        result_id = search_result["result"][0]["id"]
        
        # 启动下载
        await get_download_url(result_id, 'id', user, userID, song_name)
        
    except Exception as e:
        await danmuji.send_dm(f'搜索歌曲 {song_name} 时发生错误')
        print(f'[error] Search failed: {e}')


class bilibiliClient():
    async def startup(self):
        # 连接直播间并保持连接，直到外部中断
        await monitor.connect() 
        # 以下为测试代码
        # commentUser = "TEST3"
        # commentText = "点歌 稻香"
        # commentUserID = "1341"
        # await danmuji.pick_msg(commentUser, commentUserID, commentText)
        

    # 优化：send_dm 应该能够发送弹幕，如果不想发送，也应该保持 await 兼容
    async def send_dm(self, Text):
        print(f'[DM_SENT] {Text}')
        # pass # 保持异步兼容
        await sender.send_danmaku(Danmaku(Text))

    async def pick_msg(self, User, UserID, Text):
        
          # 获取第一个音频文件的信息
        def sync_get_current_song_info():
                files = os.listdir(f'{path}/resource/playlist')
                files.sort()  # 按文件名（下载时间）排序
                current_audio_file = None
                for f in files:
                    # 找到第一个符合音频扩展名且不是正在下载的临时文件的文件
                    if f.endswith(AUDIO_EXTENSIONS) and (f.find('.download') == -1):
                        current_audio_file = f
                        break
                if current_audio_file:
                    try:
                        base_name, _ = os.path.splitext(current_audio_file)
                        info_file_path = f'{path}/resource/playlist/{base_name}.info'
                        with open(info_file_path, 'r', encoding='utf-8') as info_file:
                            # 只获取第二行
                            info_file.readline()
                            requester_id = info_file.readline().strip()
                            return requester_id
                    except FileNotFoundError:
                        print(f"⚠️ 找不到对应的 .info 文件: {info_file_path}")
                        return ""
                    except Exception as e:
                        print(f"❌ 读取 .info 文件出错: {e}")
                        return ""
                else:
                    return "" # 播放列表为空

        global encode_lock
        global rp_lock
        # 管理员命令 (UserID='1762226' 是示例，请替换为实际管理员ID)
        if UserID == '1762226':
            if Text == '锁定':
                rp_lock = True
                await self.send_dm('已锁定点播功能，不响应任何弹幕')
                return
            elif Text == '解锁':
                rp_lock = False
                await self.send_dm('已解锁点播功能，恢复响应弹幕')
                return
            elif Text == '清空列表':
                if encode_lock:
                    await self.send_dm('有渲染任务，无法清空')
                    return
                # 将阻塞的 os.listdir 和 del_file 放在线程中运行
                def sync_clean():
                    for i in os.listdir(f'{path}/resource/playlist'):
                        del_file(i)
                
                await asyncio.to_thread(sync_clean)
                await self.send_dm('已经清空列表~')
                return
        # 点播功能检查
        if rp_lock:
            return # 如果锁定，则不响应普通弹幕
        
        #查找关键词
        keyword = '点歌'
        start_index = Text.find(keyword)
        is_playlist_empty = True  # 默认播放列表是空的
        # 检查是否找到了 "点歌" 关键词
        if start_index != -1:
            extracted_content = Text[start_index + len(keyword):].strip()
            if extracted_content:
                # 检查当前有没有点播的歌曲
                for root, dirs, files in os.walk(f'{path}/resource/playlist'):
                    for filename in files:
                        file_extension = os.path.splitext(filename)[1].lower()
                        if file_extension in AUDIO_EXTENSIONS:
                            # 找到一个音频文件，说明播放列表不为空
                            print(f"✅ 播放列表中找到音频文件: {os.path.join(root, filename)}")
                            is_playlist_empty = False
                            # 找到后立即退出两层循环，停止文件搜索
                            break
                    if not is_playlist_empty:
                        break # 退出 os.walk 的最外层循环
                # 设置首次点歌标记
                global first_order 
                first_order = is_playlist_empty     
                # 异步搜索并下载
                await search_song(extracted_content, User, UserID)
            else:
                await self.send_dm('点歌格式：点歌 [歌曲名]')

        if((Text == '点播列表') or (Text == '歌曲列表')):
            await danmuji.send_dm('已收到'+User+'的指令，正在查询')
            files = os.listdir(path+'/resource/playlist')   #获取目录下所有文件
            files.sort()    #按文件名（下载时间）排序
            songs_count = 0 #项目数量
            all_the_text = ""
            for f in files:
                if((f.endswith(AUDIO_EXTENSIONS)) and (f.find('.download') == -1)): 
                    try:
                        base_name, _ = os.path.splitext(f) 
                        info_file = open(f'{path}/resource/playlist/{base_name}.info', 'r' ,encoding='utf-8') 
                        all_the_text = info_file.readline().strip()
                        info_file.close()
                    except Exception as e:
                        print(e)
                    if(songs_count < 10):
                        await asyncio.sleep(2)
                        await danmuji.send_dm(all_the_text)
                    songs_count += 1
            if(songs_count == 0):
                await danmuji.send_dm('当前点播列表为空')
                return
            if(songs_count <= 10):
                await asyncio.sleep(2)
                await danmuji.send_dm('点播列表展示完毕，一共'+str(songs_count)+'个')
            else:
                await danmuji.send_dm('点播列表前十个展示完毕，一共'+str(songs_count)+'个')
        
        if(Text == '切歌' or Text == '下一首'):
            current_song_id = sync_get_current_song_info()
            if(current_song_id == UserID) or (current_song_id == ""):
                try:
                    with open(skip_flag_file, 'w') as f:
                        f.write('skip')
                    await self.send_dm('已发送切歌信号，请稍后')
                    print(f'[log] 收到切歌命令，已发送切歌信号')
                except Exception as e:
                    await self.send_dm('切歌失败')
                    print(f'[log] 切歌信号发送失败: {e}')
            else:
                await self.send_dm('不是你点的歌')
        
        # start_index = Text.find('mvid')
        # if start_index != -1:
        #     extracted_content = Text[start_index + len(keyword):].strip()
        #     if extracted_content:
        #         await search_song(extracted_content, User)
        #     else:
        #         await self.send_dm('点歌格式：mvid[MV网易云id]')



if __name__ == '__main__':
    try:
        danmuji = bilibiliClient()
        # 获取事件循环
        loop = asyncio.get_event_loop()
        
        # 1. 运行 startup 任务，等待连接成功
        print('正在连接弹幕服务器...')
        loop.run_until_complete(danmuji.startup())
        
        print('连接弹幕服务器成功，事件循环开始持续运行...')
        # 2. 使用 run_forever() 让事件循环持续监听弹幕
        loop.run_forever()
        
    except KeyboardInterrupt:
        print('程序被用户中断 (Ctrl+C). 正在安全退出...')
    except Exception as e:
        print(f'[error] 脚本发生错误: {e}')
        
    finally:
        # 3. 清理工作
        print('开始清理任务并关闭连接...')
        monitor.disconnect() # 确保断开 Bilibili 的连接
        
        # 取消所有仍在运行的异步任务
        pending = asyncio.all_tasks(loop)
        if pending:
            print(f'正在取消 {len(pending)} 个任务...')
            for task in pending:
                task.cancel()
            
            # 等待所有任务真正结束
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            
        # 停止并关闭循环
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
             loop.close()
        
        # 4. 自动重启
        print('尝试自动重启脚本...')
        os.system(f"python3 {config['path']}/Danmu.py")