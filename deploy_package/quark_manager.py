"""
夸克网盘文件管理器（企业微信机器人版本）
移除浏览器登录功能，改为直接接收cookie
"""
import asyncio
import json
import os
import random
import re
from typing import Any, Union, Optional, List

import httpx
from utils import custom_print, generate_random_code, get_datetime, get_timestamp, read_config, save_config

CONFIG_DIR = './config'
os.makedirs(CONFIG_DIR, exist_ok=True)


class QuarkPanFileManager:
    """夸克网盘文件管理器（无浏览器登录版本）"""
    
    def __init__(self, cookies: str = None, banned_keywords: Optional[List[str]] = None, ad_fid: str = '') -> None:
        """
        初始化文件管理器
        
        Args:
            cookies: Cookie字符串，如果为None则从配置文件读取
        """
        self.folder_id: Union[str, None] = None
        self.user: Union[str, None] = '用户A'
        self.pdir_id: Union[str, None] = '0'
        self.dir_name: Union[str, None] = '根目录'
        self.banned_keywords: List[str] = banned_keywords or []
        # 去除空白的关键词
        self.banned_keywords = [k.strip() for k in self.banned_keywords if k and k.strip()]
        self.ad_fid: str = ad_fid.strip() if ad_fid else ''
        # 最近转存的文件夹ID，最多保留5个
        self.recent_transfer_folders: List[str] = []
        
        # 如果提供了cookies，使用提供的；否则从配置文件读取
        if cookies:
            self.cookies = cookies
            # 保存cookie到配置文件
            save_config(f'{CONFIG_DIR}/cookies.txt', cookies)
        else:
            self.cookies = self.load_cookies()
        
        if not self.cookies:
            raise ValueError("Cookie不能为空，请先设置Cookie")
        
        self.headers: dict[str, str] = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko)'
                          ' Chrome/94.0.4606.71 Safari/537.36 Core/1.94.225.400 QQBrowser/12.2.5544.400',
            'origin': 'https://pan.quark.cn',
            'referer': 'https://pan.quark.cn/',
            'accept-language': 'zh-CN,zh;q=0.9',
            'cookie': self.cookies,
        }
    
    def load_cookies(self) -> str:
        """从配置文件加载Cookie"""
        try:
            cookie_path = f'{CONFIG_DIR}/cookies.txt'
            if os.path.exists(cookie_path):
                with open(cookie_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        return content
        except Exception as e:
            custom_print(f"加载Cookie失败: {str(e)}", error_msg=True)
        return ""
    
    def update_cookies(self, cookies: str) -> None:
        """更新Cookie"""
        self.cookies = cookies
        self.headers['cookie'] = cookies
        save_config(f'{CONFIG_DIR}/cookies.txt', cookies)
        custom_print("Cookie已更新")
    
    async def verify_cookies(self) -> tuple[bool, str]:
        """
        验证Cookie是否有效
        
        Returns:
            tuple: (是否有效, 用户名或错误信息)
        """
        try:
            nickname = await self.get_user_info()
            if nickname:
                return True, nickname
            else:
                return False, "无法获取用户信息，Cookie可能已失效"
        except Exception as e:
            return False, f"Cookie验证失败: {str(e)}"
    
    @staticmethod
    def get_pwd_id(share_url: str) -> str:
        """从分享链接中提取pwd_id"""
        return share_url.split('?')[0].split('/s/')[-1]
    
    async def get_stoken(self, pwd_id: str, password: str = '') -> str:
        """获取分享页面的stoken"""
        params = {
            'pr': 'ucpro',
            'fr': 'pc',
            'uc_param_str': '',
            '__dt': random.randint(100, 9999),
            '__t': get_timestamp(13),
        }
        api = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token"
        data = {"pwd_id": pwd_id, "passcode": password}
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.post(api, json=data, params=params, headers=self.headers, timeout=timeout)
            json_data = response.json()
            if json_data['status'] == 200 and json_data['data']:
                stoken = json_data["data"]["stoken"]
            else:
                stoken = ''
                custom_print(f"获取stoken失败，{json_data['message']}")
            return stoken
    
    async def get_detail(self, pwd_id: str, stoken: str, pdir_fid: str = '0') -> tuple:
        """获取分享页面详情"""
        api = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail"
        page = 1
        file_list: list[dict[str, Union[int, str]]] = []
        
        async with httpx.AsyncClient() as client:
            while True:
                params = {
                    'pr': 'ucpro',
                    'fr': 'pc',
                    'uc_param_str': '',
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    'pdir_fid': pdir_fid,
                    'force': '0',
                    "_page": str(page),
                    '_size': '50',
                    '_sort': 'file_type:asc,updated_at:desc',
                    '__dt': random.randint(200, 9999),
                    '__t': get_timestamp(13),
                }
                
                timeout = httpx.Timeout(60.0, connect=60.0)
                response = await client.get(api, headers=self.headers, params=params, timeout=timeout)
                json_data = response.json()
                
                is_owner = json_data['data']['is_owner']
                _total = json_data['metadata']['_total']
                if _total < 1:
                    return is_owner, file_list
                
                _size = json_data['metadata']['_size']
                _count = json_data['metadata']['_count']
                
                _list = json_data["data"]["list"]
                
                for file in _list:
                    d: dict[str, Union[int, str]] = {
                        "fid": file["fid"],
                        "file_name": file["file_name"],
                        "file_type": file["file_type"],
                        "dir": file["dir"],
                        "pdir_fid": file["pdir_fid"],
                        "include_items": file.get("include_items", ''),
                        "share_fid_token": file["share_fid_token"],
                        "status": file["status"]
                    }
                    file_list.append(d)
                if _total <= _size or _count < _size:
                    return is_owner, file_list
                
                page += 1
    
    async def get_user_info(self) -> str:
        """获取用户信息"""
        params = {
            'fr': 'pc',
            'platform': 'pc',
        }
        
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.get('https://pan.quark.cn/account/info', params=params,
                                        headers=self.headers, timeout=timeout)
            json_data = response.json()
            if json_data.get('data'):
                nickname = json_data['data'].get('nickname', '未知用户')
                return nickname
            else:
                return ""
    
    async def get_share_save_task_id(self, pwd_id: str, stoken: str, first_ids: list[str], 
                                     share_fid_tokens: list[str], to_pdir_fid: str = '0') -> str:
        """获取转存任务ID"""
        task_url = "https://drive.quark.cn/1/clouddrive/share/sharepage/save"
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "__dt": random.randint(600, 9999),
            "__t": get_timestamp(13),
        }
        data = {
            "fid_list": first_ids,
            "fid_token_list": share_fid_tokens,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link"
        }
        
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.post(task_url, json=data, headers=self.headers, params=params, timeout=timeout)
            json_data = response.json()
            task_id = json_data['data']['task_id']
            custom_print(f'获取任务ID：{task_id}')
            return task_id
    
    async def submit_task(self, task_id: str, retry: int = 50) -> dict:
        """提交转存任务"""
        for i in range(retry):
            await asyncio.sleep(random.randint(500, 1000) / 1000)
            custom_print(f'第{i + 1}次提交任务')
            submit_url = (f"https://drive-pc.quark.cn/1/clouddrive/task?pr=ucpro&fr=pc&uc_param_str=&task_id={task_id}"
                          f"&retry_index={i}&__dt=21192&__t={get_timestamp(13)}")
            
            async with httpx.AsyncClient() as client:
                timeout = httpx.Timeout(60.0, connect=60.0)
                response = await client.get(submit_url, headers=self.headers, timeout=timeout)
                json_data = response.json()
            
            if json_data['message'] == 'ok':
                if json_data['data']['status'] == 2:
                    if 'to_pdir_name' in json_data['data']['save_as']:
                        folder_name = json_data['data']['save_as']['to_pdir_name']
                    else:
                        folder_name = '根目录'
                    if json_data['data']['task_title'] == '分享-转存':
                        custom_print(f"结束任务ID：{task_id}")
                        custom_print(f'文件保存位置：{folder_name} 文件夹')
                    return json_data
            else:
                if json_data['code'] == 32003 and 'capacity limit' in json_data['message']:
                    custom_print("转存失败，网盘容量不足！", error_msg=True)
                    raise Exception("转存失败，网盘容量不足")
                elif json_data['code'] == 41013:
                    custom_print(f"网盘文件夹不存在，请检查保存目录设置！", error_msg=True)
                    raise Exception("网盘文件夹不存在")
                else:
                    custom_print(f"错误信息：{json_data['message']}", error_msg=True)
                    raise Exception(f"转存失败：{json_data['message']}")
        
        raise Exception("转存任务超时")
    
    async def delete_files(self, fid_list: list[str]) -> bool:
        """删除指定文件/文件夹"""
        if not fid_list:
            return True
        try:
            params = {
                'pr': 'ucpro',
                'fr': 'pc',
                'uc_param_str': '',
            }
            json_data = {
                'action_type': 2,
                'exclude_fids': [],
                'filelist': fid_list,
            }
            async with httpx.AsyncClient() as client:
                timeout = httpx.Timeout(60.0, connect=60.0)
                response = await client.post('https://drive-pc.quark.cn/1/clouddrive/file/delete',
                                             params=params, json=json_data, headers=self.headers, timeout=timeout)
                json_data = response.json()
                if json_data.get('code') == 0:
                    custom_print(f"已删除 {len(fid_list)} 个文件/文件夹")
                    return True
                custom_print(f"删除文件失败: {json_data.get('message', '未知错误')}", error_msg=True)
                return False
        except Exception as e:
            custom_print(f"删除文件异常: {str(e)}", error_msg=True)
            return False
    
    async def _filter_banned_files(self, folder_id: str) -> dict:
        """
        根据关键词递归删除转存目录中的广告文件/文件夹
        
        Returns:
            dict: {scanned, matched, deleted, all_deleted}
        """
        if not self.banned_keywords:
            return {"scanned": 0, "matched": 0, "deleted": 0, "all_deleted": False}
        stack = [folder_id]
        total_scanned = 0
        total_matched = 0
        delete_fids: List[str] = []
        
        while stack:
            current = stack.pop()
            file_list_data = await self.get_sorted_file_list(
                pdir_fid=current, page='1', size='200',
                fetch_total='false', sort='file_type:asc,updated_at:desc'
            )
            if file_list_data.get('code') != 0:
                continue
            items = file_list_data.get('data', {}).get('list', []) or []
            total_scanned += len(items)
            
            for item in items:
                name = item.get('file_name', '')
                fid = item.get('fid')
                is_dir = item.get('dir') or item.get('file_type') == 0
                
                matched = any(k.lower() in name.lower() for k in self.banned_keywords)
                if matched and fid:
                    delete_fids.append(fid)
                    total_matched += 1
                    continue
                
                # 目录未命中则继续深入
                if is_dir and fid:
                    stack.append(fid)
        
        custom_print(f"[广告过滤] 扫描 {total_scanned} 个文件/文件夹，匹配屏蔽词 {total_matched} 个")
        deleted_count = 0
        if delete_fids:
            await self.delete_files(delete_fids)
            deleted_count = len(delete_fids)
        all_deleted = total_scanned > 0 and total_scanned == total_matched
        return {
            "scanned": total_scanned,
            "matched": total_matched,
            "deleted": deleted_count,
            "all_deleted": all_deleted
        }
    
    async def scan_recent_folders_for_banned(self) -> dict:
        """扫描最近转存的几个文件夹，删除屏蔽词命中文件"""
        if not self.banned_keywords:
            return {"scanned": 0, "matched": 0, "deleted": 0, "folders": []}
        if not self.recent_transfer_folders:
            return {"scanned": 0, "matched": 0, "deleted": 0, "folders": []}
        total_scanned = 0
        total_matched = 0
        total_deleted = 0
        scanned_folders = []
        for fid in list(self.recent_transfer_folders):
            res = await self._filter_banned_files(fid)
            total_scanned += res.get("scanned", 0)
            total_matched += res.get("matched", 0)
            total_deleted += res.get("deleted", 0)
            scanned_folders.append(fid)
        return {
            "scanned": total_scanned,
            "matched": total_matched,
            "deleted": total_deleted,
            "folders": scanned_folders
        }
    
    async def save_share(self, share_url: str, folder_id: str = '0') -> dict:
        """
        转存分享链接到网盘
        
        Args:
            share_url: 分享链接
            folder_id: 目标文件夹ID，默认为'0'（根目录）
        
        Returns:
            dict: 转存结果信息
        """
        self.folder_id = folder_id
        share_url = share_url.strip()
        custom_print(f'文件分享链接：{share_url}')
        
        # 提取密码
        match_password = re.search("pwd=(.*?)(?=$|&)", share_url)
        password = match_password.group(1) if match_password else ""
        
        # 提取pwd_id
        pwd_id = self.get_pwd_id(share_url).split("#")[0]
        if not pwd_id:
            raise ValueError('文件分享链接不可为空！')
        
        # 获取stoken
        stoken = await self.get_stoken(pwd_id, password)
        if not stoken:
            raise ValueError("获取stoken失败，无法转存")
        
        # 获取文件详情
        is_owner, data_list = await self.get_detail(pwd_id, stoken)
        
        if not data_list:
            raise ValueError("分享链接中没有文件")
        
        if is_owner == 1:
            raise ValueError('网盘中已经存在该文件，无需再次转存')
        
        # 统计文件信息
        files_count = 0
        folders_count = 0
        files_list: list[str] = []
        folders_list: list[str] = []
        
        for data in data_list:
            if data['dir']:
                folders_count += 1
                folders_list.append(data["file_name"])
            else:
                files_count += 1
                files_list.append(data["file_name"])
        
        total_files_count = len(data_list)
        
        # 获取转存任务ID
        fid_list = [i["fid"] for i in data_list]
        share_fid_token_list = [i["share_fid_token"] for i in data_list]
        
        if not self.folder_id:
            raise ValueError('保存目录ID不合法，请重新设置')
        
        task_id = await self.get_share_save_task_id(pwd_id, stoken, fid_list, share_fid_token_list,
                                                    to_pdir_fid=self.folder_id)
        
        # 提交任务
        result = await self.submit_task(task_id)
        
        # 返回结果
        if 'to_pdir_name' in result['data']['save_as']:
            folder_name = result['data']['save_as']['to_pdir_name']
        else:
            folder_name = '根目录'
        
        # 过滤屏蔽词文件
        await self._filter_banned_files(self.folder_id)
        # 记录最近转存的文件夹
        if self.folder_id:
            if self.folder_id in self.recent_transfer_folders:
                self.recent_transfer_folders.remove(self.folder_id)
            self.recent_transfer_folders.insert(0, self.folder_id)
            self.recent_transfer_folders = self.recent_transfer_folders[:5]
        
        return {
            'success': True,
            'total': total_files_count,
            'files_count': files_count,
            'folders_count': folders_count,
            'files_list': files_list,
            'folders_list': folders_list,
            'folder_name': folder_name,
            'folder_id': self.folder_id,  # 保存目录ID，可用于生成整个目录的分享链接
            'fid_list': fid_list  # 原分享链接中的fid，转存后可能需要重新获取
        }
    
    async def create_dir_in_folder(self, parent_folder_id: str, dir_name: str) -> Optional[str]:
        """
        在指定文件夹中创建子文件夹
        
        Args:
            parent_folder_id: 父文件夹ID
            dir_name: 文件夹名称
        
        Returns:
            str: 新创建的文件夹ID，失败返回None
        """
        try:
            params = {
                'pr': 'ucpro',
                'fr': 'pc',
                'uc_param_str': '',
                '__dt': random.randint(100, 9999),
                '__t': get_timestamp(13),
            }
            
            json_data = {
                'pdir_fid': parent_folder_id,
                'file_name': dir_name,
                'dir_path': '',
                'dir_init_lock': False,
            }
            
            async with httpx.AsyncClient() as client:
                timeout = httpx.Timeout(60.0, connect=60.0)
                response = await client.post('https://drive-pc.quark.cn/1/clouddrive/file', 
                                             params=params, json=json_data, headers=self.headers, timeout=timeout)
                json_data = response.json()
                
                if json_data.get("code") == 0 and json_data.get("data"):
                    folder_id = json_data["data"].get("fid")
                    if folder_id:
                        custom_print(f"创建文件夹成功: {dir_name} (ID: {folder_id})")
                        return folder_id
                
                error_msg = json_data.get('message', '创建文件夹失败')
                code = json_data.get('code', '')
                if code == 23008:
                    error_msg = '文件夹同名冲突，请更换一个文件夹名称后重试'
                custom_print(f"创建文件夹失败: {error_msg}", error_msg=True)
                return None
                
        except Exception as e:
            custom_print(f"创建文件夹异常: {str(e)}", error_msg=True)
            return None

    async def delete_files(self, fid_list: list[str]) -> bool:
        """
        删除指定文件/文件夹

        Args:
            fid_list: 要删除的fid列表

        Returns:
            bool: 是否删除成功
        """
        if not fid_list:
            return True

        try:
            params = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": ""
            }
            json_data = {
                "action_type": 2,
                "exclude_fids": [],
                "filelist": fid_list,
            }
            async with httpx.AsyncClient() as client:
                timeout = httpx.Timeout(60.0, connect=60.0)
                response = await client.post(
                    "https://drive-pc.quark.cn/1/clouddrive/file/delete",
                    params=params, json=json_data, headers=self.headers, timeout=timeout
                )
                json_resp = response.json()
                if json_resp.get("code") == 0:
                    custom_print(f"删除文件成功: {len(fid_list)} 个")
                    return True
                else:
                    custom_print(f"删除文件失败: {json_resp.get('message', '未知错误')}", error_msg=True)
                    return False
        except Exception as e:
            custom_print(f"删除文件异常: {str(e)}", error_msg=True)
            return False
    
    async def get_share_task_id(self, fid: str, file_name: str, url_type: int = 1, 
                                expired_type: int = 2, password: str = '', extra_fids: Optional[List[str]] = None) -> str:
        """获取分享任务ID"""
        fid_list = [fid]
        if extra_fids:
            fid_list.extend(extra_fids)

        json_data = {
            "fid_list": fid_list,
            "title": file_name,
            "url_type": url_type,
            "expired_type": expired_type
        }
        if url_type == 2:
            if password:
                json_data["passcode"] = password
            else:
                json_data["passcode"] = generate_random_code()
        
        params = {
            'pr': 'ucpro',
            'fr': 'pc',
            'uc_param_str': '',
        }
        
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.post('https://drive-pc.quark.cn/1/clouddrive/share', params=params,
                                         json=json_data, headers=self.headers, timeout=timeout)
            json_data = response.json()
            
            # 检查响应
            if 'data' not in json_data:
                error_msg = json_data.get('message', '未知错误')
                error_code = json_data.get('code', '')
                raise Exception(f"获取分享任务ID失败：{error_msg} (错误代码: {error_code})")
            
            if 'task_id' not in json_data['data']:
                error_msg = json_data.get('message', '响应格式错误')
                raise Exception(f"获取分享任务ID失败：{error_msg}")
            
            return json_data['data']['task_id']
    
    async def get_share_id(self, task_id: str, retry: int = 30) -> str:
        """获取分享ID（可能需要等待任务完成）"""
        for i in range(retry):
            await asyncio.sleep(0.5)  # 等待0.5秒
            params = {
                'pr': 'ucpro',
                'fr': 'pc',
                'uc_param_str': '',
                'task_id': task_id,
                'retry_index': str(i),
            }
            async with httpx.AsyncClient() as client:
                timeout = httpx.Timeout(60.0, connect=60.0)
                response = await client.get('https://drive-pc.quark.cn/1/clouddrive/task', params=params,
                                            headers=self.headers, timeout=timeout)
                json_data = response.json()
                
                # 检查响应中是否有 'data' 键
                if 'data' not in json_data:
                    error_msg = json_data.get('message', '未知错误')
                    error_code = json_data.get('code', '')
                    if i == retry - 1:  # 最后一次重试
                        raise Exception(f"获取分享ID失败：{error_msg} (错误代码: {error_code})")
                    continue  # 继续重试
                
                # 检查响应是否成功
                if json_data.get('message') == 'ok':
                    data = json_data['data']
                    # 如果已经有share_id，直接返回
                    if 'share_id' in data:
                        return data['share_id']
                    # 如果任务状态不是2（处理中），继续重试
                    status = data.get('status')
                    if status != 2:
                        continue
                    # 如果状态是2但没有share_id，继续重试几次
                    if i >= 10:  # 已经重试多次后，如果还没有share_id，可能是问题
                        raise Exception("任务已完成但未返回share_id")
                    continue
                
                # 如果返回错误，且是最后一次重试，抛出异常
                if i == retry - 1:
                    error_msg = json_data.get('message', '未知错误')
                    error_code = json_data.get('code', '')
                    raise Exception(f"获取分享ID失败：{error_msg} (错误代码: {error_code})")
        
        raise Exception("获取分享ID超时")
    
    async def submit_share(self, share_id: str) -> tuple:
        """提交分享并获取分享链接"""
        params = {
            'pr': 'ucpro',
            'fr': 'pc',
            'uc_param_str': '',
        }
        
        json_data = {
            'share_id': share_id,
        }
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.post('https://drive-pc.quark.cn/1/clouddrive/share/password', params=params,
                                         json=json_data, headers=self.headers, timeout=timeout)
            json_data = response.json()
            
            # 检查响应是否成功
            if 'data' not in json_data:
                error_msg = json_data.get('message', '未知错误')
                raise Exception(f"提交分享失败：{error_msg}")
            
            if 'share_url' not in json_data['data'] or 'title' not in json_data['data']:
                error_msg = json_data.get('message', '响应格式错误')
                raise Exception(f"获取分享链接失败：{error_msg}")
            
            share_url = json_data['data']['share_url']
            title = json_data['data']['title']
            if 'passcode' in json_data['data']:
                share_url = share_url + f"?pwd={json_data['data']['passcode']}"
            return share_url, title
    
    async def get_share_task_id_multi(self, fid_list: list[str], title: str, url_type: int = 1, 
                                      expired_type: int = 2, password: str = '') -> str:
        """获取分享任务ID（支持多个文件）"""
        json_data = {
            "fid_list": fid_list,
            "title": title,
            "url_type": url_type,
            "expired_type": expired_type
        }
        if url_type == 2:
            if password:
                json_data["passcode"] = password
            else:
                json_data["passcode"] = generate_random_code()
        
        params = {
            'pr': 'ucpro',
            'fr': 'pc',
            'uc_param_str': '',
        }
        
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.post('https://drive-pc.quark.cn/1/clouddrive/share', params=params,
                                         json=json_data, headers=self.headers, timeout=timeout)
            json_data = response.json()
            
            # 检查响应
            if 'data' not in json_data:
                error_msg = json_data.get('message', '未知错误')
                error_code = json_data.get('code', '')
                raise Exception(f"获取分享任务ID失败：{error_msg} (错误代码: {error_code})")
            
            if 'task_id' not in json_data['data']:
                error_msg = json_data.get('message', '响应格式错误')
                raise Exception(f"获取分享任务ID失败：{error_msg}")
            
            return json_data['data']['task_id']
    
    async def create_share_link(self, fid: str, file_name: str, expired_type: int = 4, 
                                password: str = '', ad_fid: str = '') -> tuple:
        """
        为文件创建分享链接
        
        Args:
            fid: 文件ID
            file_name: 文件名
            expired_type: 过期类型 1=永久 2=1天 3=7天 4=30天
            password: 提取码，为空则自动生成
        
        Returns:
            tuple: (分享链接, 文件名)
        """
        extra_fids = [ad_fid] if ad_fid else None
        task_id = await self.get_share_task_id(
            fid, file_name, url_type=1, expired_type=expired_type, password=password, extra_fids=extra_fids
        )
        share_id = await self.get_share_id(task_id)
        share_url, title = await self.submit_share(share_id)
        return share_url, title
    
    async def create_share_link_multi(self, fid_list: list[str], title: str, expired_type: int = 1, 
                                      password: str = '', ad_fid: str = '') -> tuple:
        """
        为多个文件创建分享链接
        
        Args:
            fid_list: 文件ID列表
            title: 分享标题
            expired_type: 过期类型 1=永久 2=1天 3=7天 4=30天
            password: 提取码，为空则自动生成
        
        Returns:
            tuple: (分享链接, 标题)
        """
        if not fid_list:
            raise ValueError("文件ID列表不能为空")
        
        if ad_fid:
            fid_list = fid_list + [ad_fid]

        task_id = await self.get_share_task_id_multi(fid_list, title, url_type=1, 
                                                     expired_type=expired_type, password=password)
        share_id = await self.get_share_id(task_id)
        share_url, share_title = await self.submit_share(share_id)
        return share_url, share_title
    
    async def get_sorted_file_list(self, pdir_fid='0', page='1', size='100', 
                                   fetch_total='false', sort='') -> dict[str, Any]:
        """获取文件列表"""
        params = {
            'pr': 'ucpro',
            'fr': 'pc',
            'uc_param_str': '',
            'pdir_fid': pdir_fid,
            '_page': page,
            '_size': size,
            '_fetch_total': fetch_total,
            '_fetch_sub_dirs': '1',
            '_sort': sort,
            '__dt': random.randint(100, 9999),
            '__t': get_timestamp(13),
        }
        
        async with httpx.AsyncClient() as client:
            timeout = httpx.Timeout(60.0, connect=60.0)
            response = await client.get('https://drive-pc.quark.cn/1/clouddrive/file/sort', params=params,
                                        headers=self.headers, timeout=timeout)
            json_data = response.json()
            return json_data
    
    async def load_folder_id(self, renew=False) -> tuple:
        """加载文件夹ID"""
        self.user = await self.get_user_info()
        if not self.user:
            raise ValueError("无法获取用户信息，请检查Cookie")
        
        # 从配置文件读取保存目录
        try:
            json_data = read_config(f'{CONFIG_DIR}/config.json', 'json')
            if json_data:
                self.pdir_id = json_data.get('pdir_id', '0')
                self.dir_name = json_data.get('dir_name', '根目录')
        except:
            self.pdir_id = '0'
            self.dir_name = '根目录'
        
        return self.pdir_id, self.dir_name

