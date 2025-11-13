#coding:utf-8
import os
import sys
import time
import random
# 修改：不再只导入 MP3，而是导入整个 mutagen 库
import mutagen  # 
import json
#import Config
import shutil
import _thread
import service.AssMaker
import pysubs2

config = json.load(open('./Config.json', encoding='utf-8'))
path = config['path']
rtmp = config['rtmp']['url']
live_code = config['rtmp']['code']
nightvideo = bool(int(config['nightvideo']['use']))

# 新增：定义支持的音频格式元组
AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac')

def get_audio_title(filepath):
    """
    使用 mutagen 智能获取音频文件的标题。
    """
    try:
        audio = mutagen.File(filepath)

        if audio is None:
            return f"文件无法加载: {filepath}"

        # 1. 尝试 MP3 (ID3)
        if 'TIT2' in audio:
            # TIT2 返回一个 TXXX Frame 对象，需要 .text
            return audio['TIT2'].text[0]
        # if '©nam' in audio:
        #     # ©nam 返回一个列表
        #     return audio['©nam'][0]
        if 'title' in audio:
            # 'title' (Vorbis) 也返回一个列表
            return audio['title'][0]

        return "no tile"

    except mutagen.MutagenError as e:
        print(f"Mutagen 加载错误: {e}")
    except KeyError:
        print("找到了标签类型，但标题键不存在")
    except IndexError:
        print("找到了标题键，但内容为空")
    except Exception as e:
        print(f"发生未知错误: {e}")
    

def modify_ass_by_title(output_path, new_text):
    try:
        subs = pysubs2.load(r'./default.ass', encoding="utf-8")
        new_line = pysubs2.SSAEvent(layer=2,start=0, end=3600000, text=new_text,style='Title')
        subs.append(new_line)       
        # 4. 保存修改后的文件
        subs.save(output_path)
        print(f"修改后的 ASS 文件已保存到：{output_path}")
    except FileNotFoundError:
        print(f"错误：找不到文件")
    except Exception as e:
        print(f"处理文件时发生错误: {e}")

#格式化时间，暂时没啥用，以后估计也没啥用
def convert_time(n):
    s = n%60
    m = int(n/60)
    return '00:'+"%02d"%m+':'+"%02d"%s

#移动放完的视频到缓存文件夹
def remove_v(filename):
    try:
        #shutil.move(path+'/resource/playlist/'+filename,path+'/resource/music/')
        os.remove(path+'/resource/playlist/'+filename)
    except Exception as e:
        print(e)
    try:
        # 修改：使用 os.path.splitext 安全地替换扩展名
        base_name = os.path.splitext(filename)[0] # 
        os.remove(path+'/resource/playlist/'+base_name+'ok.ass')
        os.remove(path+'/resource/playlist/'+base_name+'ok.info')
    except Exception as e:
        print(e)
        print('delete error')

while True:
    try:
        if (time.localtime()[3] <= 5) and nightvideo: #time.localtime()[3] >= 23 or 
            print('night is comming~')  #晚上到咯~
            night_files = os.listdir(path+'/resource/night') #获取所有缓存文件
            night_files.sort()    #排序文件
         
            night_ran = random.randint(0,len(night_files)-1)    #随机抽一个文件
            # if(night_files[night_ran].find('.flv') != -1):  #如果为flv视频
                # #直接暴力推流
                # print('ffmpeg -threads 1 -re -i "'+path+"/resource/night/"+night_files[night_ran]+'" -vcodec copy -acodec copy -f flv "'+rtmp+live_code+'"')
                # os.system('ffmpeg -threads 1 -re -i "'+path+"/resource/night/"+night_files[night_ran]+'" -vcodec copy -acodec copy -f 
            
            # 修改：使用 .endswith(AUDIO_EXTENSIONS) 检查
            if(night_files[night_ran].endswith(AUDIO_EXTENSIONS)):  #如果为音频 
                pic_files = os.listdir(path+'/resource/img') #获取准备的图片文件夹中的所有图片
                pic_files.sort()    #排序数组
                pic_ran = random.randint(0,len(pic_files)-1)    #随机选一张图片
                
                # 修改：使用 mutagen.File 通用加载器
                audio = mutagen.File(path+'/resource/night/'+night_files[night_ran]) # 
                seconds=audio.info.length   #获取时长 
                print('mp3 long:'+convert_time(seconds))
                
                # 修改：使用 os.path.splitext 安全地获取基础文件名
                base_name = os.path.splitext(night_files[night_ran])[0] # 
                
                if not os.path.isfile(path+'/resource/night/'+base_name+'.ass'):
                    service.AssMaker.make_ass(path+'/night/'+base_name,'当前是晚间专属时间哦~时间范围：凌晨0-5点\\N大家晚安哦~做个好梦~\\N当前文件名：'+night_files[night_ran],path)
                
                print('ffmpeg -threads 1 -re -loop 1 -r 15 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/night/'+night_files[night_ran]+'" -vf ass="'+path+'/resource/night/'+base_name+'.ass" -x264-params "profile=high:level=5.1" -pix_fmt yuv420p -b '+config['rtmp']['bitrate']+'k -vcodec libx264 -acodec copy -f flv "'+rtmp+live_code+'"')
                os.system('ffmpeg -threads 1 -re -loop 1 -r 15 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/night/'+night_files[night_ran]+'" -vf ass="'+path+'/resource/night/'+base_name+'.ass" -x264-params "profile=high:level=5.1" -pix_fmt yuv420p -b '+config['rtmp']['bitrate']+'k -vcodec libx264 -acodec copy -f flv "'+rtmp+live_code+'"') # 
            continue
        
        files = os.listdir(path+'/resource/playlist')   #获取文件夹下全部文件
 
        files.sort()    #排序文件，按文件名（点播时间）排序 
        count=0     #总共匹配到的点播文件统计
        for f in files:
            # 修改：使用 .endswith(AUDIO_EXTENSIONS) 检查
            if(f.endswith(AUDIO_EXTENSIONS) and (f.find('.download') == -1)): #如果是音频文件 
                print(path+'/resource/playlist/'+f)
                seconds = 600
            
                bitrate = 0
                try:
                    # 修改：使用 mutagen.File 通用加载器
                    audio = mutagen.File(path+'/resource/playlist/'+f)   #获取文件信息 
                    seconds=audio.info.length   #获取时长 
                    bitrate=audio.info.bitrate  #获取码率 
                    title = get_audio_title(path+'/resource/playlist/'+f)
        
                    print(audio.info.length)
                except Exception as e:
                    print(e)
                    bitrate = 99999999999
                
           
                print('mp3 long:'+convert_time(seconds))
                if((seconds > 600) | (bitrate > 400000)):  #大于十分钟就不播放/码率限制400k以下 
                    print('too long/too big,delete')
                else:
                    pic_files = os.listdir(path+'/resource/img') #获取准备的图片文件夹中的所有图片
                    pic_files.sort()    #排序数组
           
                    pic_ran = random.randint(0,len(pic_files)-1)    #随机选一张图片 
                    
                    # 修改：使用 os.path.splitext 安全地获取基础文件名
                    base_name = os.path.splitext(f)[0] # 
                    
                    #推流
                    print('ffmpeg -threads 1 -re -loop 1 -r 15 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/playlist/'+f+'" -vf ass="'+path+"/resource/playlist/"+base_name+'.ass'+'" -x264-params "profile=high:level=5.1" -pix_fmt yuv420p -b '+config['rtmp']['bitrate']+'k -vcodec libx264 -acodec copy -f flv "'+rtmp+live_code+'"')
               
                    os.system('ffmpeg -threads 1 -re -loop 1 -r 15 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/playlist/'+f+'" -vf ass="'+path+"/resource/playlist/"+base_name+'.ass'+'" -x264-params "profile=high:level=5.1" -pix_fmt yuv420p -b '+config['rtmp']['bitrate']+'k -vcodec libx264 -acodec copy -f flv "'+rtmp+live_code+'"') # 
                    try:    #放完后删除文件、删除字幕、删除点播信息
                        shutil.move(path+'/resource/playlist/'+f,path+'/resource/music/')
                 
                        # 修改：使用 os.path.splitext 安全地获取基础文件名
                        shutil.move(path+'/resource/playlist/'+base_name+'.ass',path+'/resource/music/') # 
                        #os.remove(path+'/resource/playlist/'+f)
                        #os.remove(path+'/resource/playlist/'+f.replace(".mp3",'')+'.ass')
                    except Exception as e:
                      
                        print(e)
                try:
                    # 修改：使用 os.path.splitext 安全地获取基础文件名
                    base_name = os.path.splitext(f)[0] # 
                    os.remove(path+'/resource/playlist/'+base_name+'.info')
                    os.remove(path+'/resource/playlist/'+f)
                    os.remove(path+'/resource/playlist/'+base_name+'.ass')
                except:
      
                    print('delete error')
                count+=1    #点播统计加一
                break
            if((f.find('ok.flv') != -1) and (f.find('.download') == -1) and (f.find('rendering') == -1)):   #如果是有ok标记的flv文件
                print('flv:'+f)
        
                #直接推流
                print('ffmpeg -threads 1 -re -i "'+path+"/resource/playlist/"+f+'" -vcodec copy -acodec copy -f flv "'+rtmp+live_code+'"')
                os.system('ffmpeg -threads 1 -re -i "'+path+"/resource/playlist/"+f+'" -vcodec copy -acodec copy -f flv "'+rtmp+live_code+'"')
                os.rename(path+'/resource/playlist/'+f,path+'/resource/playlist/'+f.replace("ok",""))   #修改文件名，以免下次循环再次匹配
                _thread.start_new_thread(remove_v, (f.replace("ok",""),))   #异步搬走文件，以免推流卡顿 
                count+=1    #点播统计加一
                break
        if(count == 0):     #点播统计为0，说明点播的都放完了
            print('no media')
            mp3_files = os.listdir(path+'/resource/music') #获取所有缓存文件
            mp3_files.sort()    #排序文件
  
            mp3_ran = random.randint(0,len(mp3_files)-1)    #随机抽一个文件 
            
            # 修改：使用 .endswith(AUDIO_EXTENSIONS) 检查
            if(mp3_files[mp3_ran].endswith(AUDIO_EXTENSIONS)):  #如果是音频文件 
                pic_files = os.listdir(path+'/resource/img') #获取准备的图片文件夹中的所有图片
                pic_files.sort()    #排序数组
                pic_ran = random.randint(0,len(pic_files)-1)    #随机选一张图片 
                
                # 修改：使用 mutagen.File 通用加载器
                audio = mutagen.File(path+'/resource/music/'+mp3_files[mp3_ran])    #获取文件信息
                seconds=audio.info.length   #获取时长
                title = get_audio_title(path+'/resource/music/'+mp3_files[mp3_ran])
                print(f'mp3 title: {title} long:{convert_time(seconds)}')
                
                # 修改：使用 os.path.splitext 安全地获取基础文件名
                base_name = os.path.splitext(mp3_files[mp3_ran])[0] # 
                
                #推流
                ffmpeg_command=""
     
                if(os.path.isfile(path+'/resource/music/'+base_name+'.ass')):
                    if os.path.isfile(path+"/resource/music/"+base_name+'.jpg'):
                        print('ffmpeg -threads 0 -re -loop 1 -r 24 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+"/resource/music/"+base_name+'.jpg'+'" -filter_complex "[0:v][1:v]overlay=30:390[cover];[cover]ass=filename='+path.replace("\\","/").replace("C:","C\\\\\\:")+"/resource/music/"+base_name+'.ass'+'[result]" -i "'+path+'/resource/music/'+mp3_files[mp3_ran]+'" -map "[result]" -map 2,0 -pix_fmt yuv420p -preset ultrafast -maxrate '+config['rtmp']['bitrate']+'k -acodec copy -c:v libx264 -f flv "'+rtmp+live_code+'"')
        
                        os.system('ffmpeg -threads 0 -re -loop 1 -r 24 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+"/resource/music/"+base_name+'.jpg'+'" -filter_complex "[0:v][1:v]overlay=30:390[cover];[cover]ass=filename='+path.replace("\\","/").replace("C:","C\\\\\\:")+"/resource/music/"+base_name+'.ass'+'[result]" -i "'+path+'/resource/music/'+mp3_files[mp3_ran]+'" -map "[result]" -map 2,0 -pix_fmt yuv420p -preset ultrafast -maxrate '+config['rtmp']['bitrate']+'k -acodec copy -c:v libx264 -f flv "'+rtmp+live_code+'"') # 
                    else:
                        print('ffmpeg -threads 0 -re -loop 1 -r 24 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/music/'+mp3_files[mp3_ran]+'" -vf ass=filename="'+path.replace("\\","/").replace("C:","C\\\\\\:")+"/resource/music/"+base_name+'.ass'+'" -pix_fmt yuv420p -preset ultrafast -maxrate '+config['rtmp']['bitrate']+'k -acodec copy -c:v libx264 -f flv "'+rtmp+live_code+'"')
                        os.system('ffmpeg -threads 0 -re -loop 1 -r 2 -t '+str(int(seconds))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/music/'+mp3_files[mp3_ran]+'" -vf ass=filename="'+path.replace("\\","/").replace("C:","C\\\\\\:")+"/resource/music/"+base_name+'.ass'+'" -pix_fmt yuv420p -preset ultrafast -maxrate '+config['rtmp']['bitrate']+'k -acodec copy -c:v libx264 -f flv "'+rtmp+live_code+'"') # 
                else:
                    modify_ass_by_title("/tmp/temp.ass",title)
                    ffmpeg_command = 'ffmpeg -threads 0 -re -loop 1 -r 4 -t '+str(int(seconds + 5))+' -f image2 -i "'+path+'/resource/img/'+pic_files[pic_ran]+'" -i "'+path+'/resource/music/'+mp3_files[mp3_ran]+'" -vf ass=filename="/tmp/temp.ass" -pix_fmt yuv420p -preset veryfast -maxrate '+config['rtmp']['bitrate']+'k -c:a aac -b:a 320k -ar 44100 -bufsize 320k -c:v libx264 -g 4 -crf 33 -f flv "'+rtmp+live_code+'"' # 
                    print(ffmpeg_command)
                    os.system(ffmpeg_command) 
         
            if(mp3_files[mp3_ran].find('.flv') != -1):  #如果为flv视频 
                #直接推流
                print('ffmpeg -threads 0 -re -i "'+path+"/resource/music/"+mp3_files[mp3_ran]+'" -vcodec copy -acodec copy -f flv "'+rtmp+live_code+'"')
                os.system('ffmpeg -threads 0 -re -i "'+path+"/resource/music/"+mp3_files[mp3_ran]+'" -vcodec copy -acodec copy -f flv "'+rtmp+live_code+'"')
    except Exception as e:
        print(e)