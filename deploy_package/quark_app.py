"""
夸克网盘企业微信应用主程序
通过企业微信应用接收用户消息，实现自动转存和生成分享链接
"""
import asyncio
import json
import os
import re
import hashlib
import base64
import struct
import socket
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from typing import Optional
import xml.etree.ElementTree as ET

from quark_manager import QuarkPanFileManager, CONFIG_DIR
from wechat_app import WeChatApp
from utils import custom_print, get_datetime, read_config, save_config

# 尝试导入WXBizMsgCrypt用于消息加解密
try:
    from WXBizMsgCrypt3 import WXBizMsgCrypt
    WXBIZ_MSG_CRYPT_AVAILABLE = True
except ImportError:
    WXBIZ_MSG_CRYPT_AVAILABLE = False
    custom_print("警告: WXBizMsgCrypt3.py未找到，无法使用消息加解密功能", error_msg=True)

# 尝试导入httpx用于消息转发
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    custom_print("警告: httpx未安装，微信消息转发功能将不可用。请运行: pip install httpx", error_msg=True)

# 尝试导入AES解密所需的库
try:
    from Crypto.Cipher import AES
    AES_AVAILABLE = True
except ImportError:
    AES_AVAILABLE = False
    custom_print("警告: pycryptodome未安装，无法使用EncodingAESKey解密功能。请运行: pip install pycryptodome", error_msg=True)


class QuarkAppHandler:
    """夸克网盘应用处理器（基于企业微信应用）"""
    
    def __init__(self, corp_id: str, agent_id: str, secret: str, 
                 default_folder_id: str = '0', search_folder_id: str = '0',
                 proxy: Optional[str] = None,
                 banned_keywords: Optional[list[str]] = None,
                 ad_fid: str = ''):
        """
        初始化应用处理器
        
        Args:
            corp_id: 企业ID
            agent_id: 应用ID
            secret: 应用密钥
            default_folder_id: 默认保存文件夹ID，默认为'0'（根目录）
            search_folder_id: 默认搜索文件夹ID，默认为'0'（根目录）
            proxy: 微信消息转发代理地址（可选，默认：https://qyapi.weixin.qq.com）
                   2022年6月20日后创建的自建应用才需要配置代理
        """
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.default_folder_id = default_folder_id
        self.search_folder_id = search_folder_id
        self.app = WeChatApp(corp_id, agent_id, secret, proxy=proxy)
        self.manager: Optional[QuarkPanFileManager] = None
        self.user_search_results: dict[str, dict] = {}  # 按用户ID存储搜索结果
        self.user_search_mode: dict[str, bool] = {}  # 按用户ID存储搜索模式状态
        self.user_transfer_share_mode: dict[str, bool] = {}  # 按用户ID存储转存分享模式状态（False=只转存不分享，True=转存后分享）
        self.banned_keywords = [k.strip() for k in (banned_keywords or []) if k.strip()]
        self.ad_fid = ad_fid.strip()
        # 记录等待用户输入屏蔽词的状态
        self.user_waiting_ban_input: dict[str, bool] = {}
        self._load_manager()

    def _update_banned_keywords(self, new_keywords: list[str]) -> None:
        """更新内存与配置文件的屏蔽词"""
        merged = set(self.banned_keywords)
        for k in new_keywords:
            k = k.strip()
            if k:
                merged.add(k)
        self.banned_keywords = [k for k in merged if k]
        if self.manager:
            self.manager.banned_keywords = self.banned_keywords
        # 写回配置文件
        try:
            config_path = os.path.join(CONFIG_DIR, 'bot_config.json')
            cfg = read_config(config_path, 'json')
            cfg['quark_banned'] = ",".join(self.banned_keywords)
            save_config(config_path, json.dumps(cfg, ensure_ascii=False, indent=2))
            custom_print(f"屏蔽词已更新并保存：{cfg['quark_banned']}")
        except Exception as e:
            custom_print(f"保存屏蔽词到配置失败: {str(e)}", error_msg=True)
    
    def _load_manager(self):
        """加载文件管理器"""
        try:
            self.manager = QuarkPanFileManager(
                banned_keywords=self.banned_keywords,
                ad_fid=self.ad_fid
            )
            custom_print("文件管理器加载成功")
        except Exception as e:
            custom_print(f"文件管理器加载失败: {str(e)}", error_msg=True)
            self.manager = None
    
    async def set_cookie(self, cookie: str, touser: Optional[str] = None) -> dict:
        """
        设置Cookie
        
        Args:
            cookie: Cookie字符串
            touser: 接收消息的用户ID
        
        Returns:
            dict: 处理结果
        """
        try:
            # 创建新的管理器验证cookie
            test_manager = QuarkPanFileManager(
                cookies=cookie,
                banned_keywords=self.banned_keywords,
                ad_fid=self.ad_fid
            )
            is_valid, user_info = await test_manager.verify_cookies()
            
            if is_valid:
                # Cookie有效，更新管理器
                self.manager = test_manager
                # 同步最近转存目录
                self.manager.recent_transfer_folders = self.manager.recent_transfer_folders[:5]
                self.app.send_success("Cookie设置成功", f"用户：{user_info}\nCookie已保存并验证通过", touser=touser)
                return {'success': True, 'message': f'Cookie设置成功，用户：{user_info}'}
            else:
                self.app.send_error("Cookie设置失败", user_info, touser=touser)
                return {'success': False, 'message': user_info}
        except Exception as e:
            error_msg = f"设置Cookie时发生错误：{str(e)}"
            self.app.send_error("Cookie设置失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
    
    async def process_text_with_links(self, text: str, touser: Optional[str] = None, generate_share: Optional[bool] = None) -> dict:
        """
        处理包含多个链接的文本（使用简洁逻辑，参考 quark666_副本.py）
        
        Args:
            text: 包含链接的文本
            touser: 接收消息的用户ID
            generate_share: 是否生成分享链接（None时根据用户模式决定）
        
        Returns:
            dict: 处理结果
        """
        if not self.manager:
            error_msg = "文件管理器未初始化，请先设置Cookie"
            self.app.send_error("处理失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
        
        try:
            # 验证Cookie是否有效
            is_valid, user_info = await self.manager.verify_cookies()
            if not is_valid:
                error_msg = f"Cookie已失效，请重新设置Cookie\n{user_info}"
                self.app.send_warning("Cookie失效", error_msg, touser=touser)
                return {'success': False, 'message': error_msg, 'cookie_expired': True}
            
            # 提取所有夸克网盘链接
            url_pattern = r'https?://pan\.quark\.cn/s/[^\s\)]+'
            urls = re.findall(url_pattern, text)
            
            if not urls:
                error_msg = "文本中未找到夸克网盘链接"
                self.app.send_error("处理失败", error_msg, touser=touser)
                return {'success': False, 'message': error_msg}
            
            # 获取保存目录
            parent_folder_id, parent_folder_name = await self.manager.load_folder_id()
            if not parent_folder_id or parent_folder_id == '0':
                parent_folder_id = self.default_folder_id
            
            # 判断是否需要生成分享链接
            user_key = touser if touser else 'default'
            if generate_share is None:
                generate_share = self.user_transfer_share_mode.get(user_key, False)  # 默认False，只转存不分享
            custom_print(f"[批量转存] 用户: {user_key}, 转存分享模式状态: {generate_share}, 所有用户状态: {self.user_transfer_share_mode}")
            
            # 逐个处理链接（使用简洁逻辑，不重试，不等待）
            # 收集所有链接的替换信息
            result_text = text  # 保持原文结构
            link_replacements = []  # 存储 (原链接, 新链接) 的对应关系
            success_count = 0
            
            for url in urls:
                try:
                    # 如果需要在转存分享模式下处理混合文件/文件夹，先检查文件结构
                    match_password = re.search("pwd=(.*?)(?=$|&)", url)
                    password = match_password.group(1) if match_password else ""
                    pwd_id = self.manager.get_pwd_id(url).split("#")[0]
                    
                    current_folder_id = parent_folder_id
                    need_create_folder = False
                    folder_name_new = ''
                    if generate_share and pwd_id:
                        try:
                            stoken = await self.manager.get_stoken(pwd_id, password)
                            is_owner, data_list = await self.manager.get_detail(pwd_id, stoken)
                            if data_list:
                                files_count = sum(1 for d in data_list if not d.get('dir', False))
                                folders_count = sum(1 for d in data_list if d.get('dir', False))
                                # 如果同时有文件和文件夹，需要创建新文件夹
                                if files_count > 0 and folders_count > 0:
                                    need_create_folder = True
                                    # 生成文件夹名称（使用时间戳，格式：转存_20260105_233853）
                                    from utils import get_datetime
                                    folder_name_new = f"转存_{get_datetime(fmt='%Y%m%d_%H%M%S')}"
                                    new_folder_id = await self.manager.create_dir_in_folder(parent_folder_id, folder_name_new)
                                    if new_folder_id:
                                        current_folder_id = new_folder_id
                                        custom_print(f"检测到混合文件/文件夹，已创建新文件夹: {folder_name_new} (ID: {new_folder_id})")
                                    else:
                                        custom_print(f"创建新文件夹失败，使用原文件夹", error_msg=True)
                                        need_create_folder = False
                        except Exception as e:
                            custom_print(f"检查文件结构失败: {str(e)}，使用默认转存逻辑", error_msg=True)
                    
                    # 转存文件
                    result = await self.manager.save_share(url, current_folder_id)
                    success_count += 1

                    # 过滤广告文件（仅对本次转存的文件名生效）
                    saved_names = (result.get('files_list', []) or []) + (result.get('folders_list', []) or [])
                    await self._filter_banned_files(current_folder_id, saved_names)
                    
                    # 生成分享链接（仅在转存分享模式下）
                    new_link = url  # 默认使用原链接
                    if generate_share:
                        try:
                            custom_print(f"[转存分享] 开始生成分享链接，转存文件夹ID: {current_folder_id}")
                            # 等待一下，确保文件已经转存完成
                            await asyncio.sleep(2)  # 增加等待时间，确保转存完成
                            
                            if need_create_folder:
                                # 如果创建了新文件夹，需要获取文件夹内所有文件/文件夹ID，然后生成分享链接
                                custom_print(f"[转存分享] 获取新文件夹内的文件列表: {folder_name_new}")
                                file_list_data = await self.manager.get_sorted_file_list(
                                    pdir_fid=current_folder_id, page='1', size='100', 
                                    fetch_total='false', sort='file_type:asc,updated_at:desc'
                                )
                                if file_list_data.get('code') == 0 and file_list_data.get('data', {}).get('list'):
                                    # 提取所有文件/文件夹的fid
                                    all_fids = [item['fid'] for item in file_list_data['data']['list']]
                                    if all_fids:
                                        # 使用所有文件/文件夹ID生成分享链接
                                        share_url_new, title = await self.manager.create_share_link_multi(
                                            all_fids, folder_name_new, expired_type=1, password='', ad_fid=self.ad_fid
                                        )
                                        new_link = share_url_new
                                        link_replacements.append((url, new_link))
                                        custom_print(f"[转存分享] 成功生成新文件夹内所有文件的分享链接: {share_url_new}")
                                    else:
                                        custom_print(f"[转存分享] 新文件夹内没有文件，无法生成分享链接", error_msg=True)
                                        link_replacements.append((url, url))
                                else:
                                    custom_print(f"[转存分享] 获取新文件夹文件列表失败，无法生成分享链接", error_msg=True)
                                    link_replacements.append((url, url))
                            else:
                                # 没有创建新文件夹，通过查询转存目标文件夹来获取转存后的文件
                                custom_print(f"[转存分享] 查询转存目标文件夹内的文件: {current_folder_id}")
                                file_list_data = await self.manager.get_sorted_file_list(
                                    pdir_fid=current_folder_id, page='1', size='100', 
                                    fetch_total='false', sort='file_type:asc,updated_at:desc'
                                )
                                
                                if file_list_data.get('code') == 0 and file_list_data.get('data', {}).get('list'):
                                    # 获取转存的文件名列表
                                    saved_files = result.get('files_list', [])
                                    saved_folders = result.get('folders_list', [])
                                    
                                    # 在文件列表中查找匹配的文件/文件夹（按名称匹配，取最新的）
                                    matched_items = []
                                    for item in file_list_data['data']['list']:
                                        item_name = item.get('file_name', '')
                                        # 检查是否是刚转存的文件（在转存列表中）
                                        if item_name in saved_files or item_name in saved_folders:
                                            matched_items.append(item)
                                    
                                    if matched_items:
                                        # 使用第一个匹配的文件/文件夹生成分享链接
                                        first_item = matched_items[0]
                                        first_fid = first_item['fid']
                                        first_file_name = first_item['file_name']
                                        is_folder = first_item.get('dir', False) or first_item.get('file_type') == 0
                                        
                                        custom_print(f"[转存分享] 找到转存的文件: {first_file_name} (ID: {first_fid})")
                                        share_url_new, title = await self.manager.create_share_link(
                                            first_fid, first_file_name, expired_type=1, password='', ad_fid=self.ad_fid
                                        )
                                        new_link = share_url_new
                                        link_replacements.append((url, new_link))
                                        custom_print(f"[转存分享] 成功生成分享链接: {share_url_new}")
                                    else:
                                        custom_print(f"[转存分享] 未找到转存的文件，使用原链接", error_msg=True)
                                        link_replacements.append((url, url))
                                else:
                                    custom_print(f"[转存分享] 获取文件夹文件列表失败，无法生成分享链接", error_msg=True)
                                    link_replacements.append((url, url))
                        except Exception as e:
                            custom_print(f"[转存分享] 生成分享链接失败: {str(e)}", error_msg=True)
                            import traceback
                            custom_print(traceback.format_exc(), error_msg=True)
                            link_replacements.append((url, url))  # 失败时保留原链接
                    else:
                        # 只转存模式，保留原链接
                        link_replacements.append((url, url))
                    
                    # 替换链接（保持原文结构）
                    result_text = result_text.replace(url, new_link, 1)
                    
                except Exception as e:
                    custom_print(f"处理链接 {url} 失败: {str(e)}", error_msg=True)
                    link_replacements.append((url, url))  # 失败时保留原链接
            
            # 所有链接处理完毕，统一发送结果
            if generate_share:
                # 转存分享模式：发送两条消息
                # 第一条：转存完成消息（包含时间戳）
                result_msg = f"处理链接数：{len(urls)}\n"
                result_msg += f"成功转存：{success_count} 个"
                self.app.send_success("批量转存完成", result_msg, touser=touser)
                
                # 第二条：完整的文章结构（不包含时间戳，方便直接复制分享）
                self.app.send_text_message(result_text, touser=touser)
                
                # 自动退出转存分享模式（本次任务完成）
                self.user_transfer_share_mode[user_key] = False
                custom_print(f"用户 {user_key} 已退出转存分享模式（本次批量转存任务完成）")
            else:
                # 只转存模式：只发送转存完成消息
                result_msg = f"处理链接数：{len(urls)}\n"
                result_msg += f"成功转存：{success_count} 个"
                self.app.send_success("批量转存完成", result_msg, touser=touser)
            
            return {
                'success': True,
                'message': '批量转存完成',
                'processed_count': len(urls),
                'success_count': success_count
            }
            
        except Exception as e:
            error_msg = f"处理文本失败：{str(e)}"
            custom_print(error_msg, error_msg=True)
            self.app.send_error("处理失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}

    async def _filter_banned_files(self, folder_id: str, saved_names: list[str]):
        """过滤广告文件（按配置关键词，仅针对本次转存的文件名）"""
        if not self.banned_keywords or not saved_names:
            return
        try:
            file_list_data = await self.manager.get_sorted_file_list(
                pdir_fid=folder_id, page='1', size='200',
                fetch_total='false', sort='file_type:asc,updated_at:desc'
            )
            if file_list_data.get('code') != 0:
                custom_print(f"[广告过滤] 获取文件列表失败，跳过过滤：{file_list_data.get('message', '未知错误')}", error_msg=True)
                return
            items = file_list_data.get('data', {}).get('list', [])
            fids_to_delete = []
            for item in items:
                name = item.get('file_name', '')
                if name in saved_names and any(k in name for k in self.banned_keywords):
                    fids_to_delete.append(item.get('fid'))
            if fids_to_delete:
                await self.manager.delete_files(fids_to_delete)
                custom_print(f"[广告过滤] 已删除包含广告关键词的文件: {len(fids_to_delete)} 个")
        except Exception as e:
            custom_print(f"[广告过滤] 过滤广告文件异常: {str(e)}", error_msg=True)
    
    async def process_share_url(self, share_url: str, original_text: Optional[str] = None, touser: Optional[str] = None) -> dict:
        """
        处理分享链接：转存并生成分享链接（使用简洁逻辑，参考 quark666_副本.py）
        
        Args:
            share_url: 夸克网盘分享链接
            original_text: 原始文本内容（用于保留原文章结构，可选）
            touser: 接收消息的用户ID
        
        Returns:
            dict: 处理结果
        """
        if not self.manager:
            error_msg = "文件管理器未初始化，请先设置Cookie"
            self.app.send_error("处理失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
        
        try:
            # 验证Cookie是否有效
            is_valid, user_info = await self.manager.verify_cookies()
            if not is_valid:
                error_msg = f"Cookie已失效，请重新设置Cookie\n{user_info}"
                self.app.send_warning("Cookie失效", error_msg, touser=touser)
                return {'success': False, 'message': error_msg, 'cookie_expired': True}
            
            # 获取保存目录
            folder_id, folder_name = await self.manager.load_folder_id()
            if not folder_id or folder_id == '0':
                folder_id = self.default_folder_id
            
            # 判断是否需要生成分享链接
            user_key = touser if touser else 'default'
            generate_share = self.user_transfer_share_mode.get(user_key, False)  # 默认False，只转存不分享
            custom_print(f"[转存分享] 用户: {user_key}, 转存分享模式状态: {generate_share}, 所有用户状态: {self.user_transfer_share_mode}")
            
            # 如果需要在转存分享模式下处理混合文件/文件夹，先检查文件结构
            # 提取密码和pwd_id
            match_password = re.search("pwd=(.*?)(?=$|&)", share_url)
            password = match_password.group(1) if match_password else ""
            pwd_id = self.manager.get_pwd_id(share_url).split("#")[0]
            
            # 如果需要在转存分享模式下处理混合情况，先获取文件详情
            need_create_folder = False
            if generate_share and pwd_id:
                try:
                    stoken = await self.manager.get_stoken(pwd_id, password)
                    is_owner, data_list = await self.manager.get_detail(pwd_id, stoken)
                    if data_list:
                        files_count = sum(1 for d in data_list if not d.get('dir', False))
                        folders_count = sum(1 for d in data_list if d.get('dir', False))
                        # 如果同时有文件和文件夹，需要创建新文件夹
                        if files_count > 0 and folders_count > 0:
                            need_create_folder = True
                            # 生成文件夹名称（使用时间戳，格式：转存_20260105_233853）
                            from utils import get_datetime
                            folder_name_new = f"转存_{get_datetime(fmt='%Y%m%d_%H%M%S')}"
                            new_folder_id = await self.manager.create_dir_in_folder(folder_id, folder_name_new)
                            if new_folder_id:
                                folder_id = new_folder_id
                                custom_print(f"检测到混合文件/文件夹，已创建新文件夹: {folder_name_new} (ID: {new_folder_id})")
                            else:
                                custom_print(f"创建新文件夹失败，使用原文件夹", error_msg=True)
                                need_create_folder = False
                except Exception as e:
                    custom_print(f"检查文件结构失败: {str(e)}，使用默认转存逻辑", error_msg=True)
            
            # 转存文件（使用简洁逻辑，submit_task 内部有必要的轮询，但不会多次尝试生成分享链接）
            result = await self.manager.save_share(share_url, folder_id)

            # 过滤广告文件（仅针对本次转存的文件名）
            saved_names = (result.get('files_list', []) or []) + (result.get('folders_list', []) or [])
            await self._filter_banned_files(folder_id, saved_names)
            
            # 生成分享链接（仅在转存分享模式下）
            share_info = []
            
            if generate_share:
                try:
                    custom_print(f"[转存分享] 开始生成分享链接，转存文件夹ID: {folder_id}")
                    # 等待一下，确保文件已经转存完成
                    await asyncio.sleep(2)  # 增加等待时间，确保转存完成
                    
                    if need_create_folder:
                        # 如果创建了新文件夹，需要获取文件夹内所有文件/文件夹ID，然后生成分享链接
                        custom_print(f"[转存分享] 获取新文件夹内的文件列表: {folder_name_new}")
                        file_list_data = await self.manager.get_sorted_file_list(
                            pdir_fid=folder_id, page='1', size='100', 
                            fetch_total='false', sort='file_type:asc,updated_at:desc'
                        )
                        if file_list_data.get('code') == 0 and file_list_data.get('data', {}).get('list'):
                            # 提取所有文件/文件夹的fid
                            all_fids = [item['fid'] for item in file_list_data['data']['list']]
                            if all_fids:
                                # 使用所有文件/文件夹ID生成分享链接
                                share_url_new, title = await self.manager.create_share_link_multi(
                                    all_fids, folder_name_new, expired_type=1, password='', ad_fid=self.ad_fid
                                )
                                icon = "📁"
                                share_info.append({
                                    'title': title,
                                    'url': share_url_new,
                                    'icon': icon
                                })
                                custom_print(f"[转存分享] 成功生成新文件夹内所有文件的分享链接: {share_url_new}")
                            else:
                                custom_print(f"[转存分享] 新文件夹内没有文件，无法生成分享链接", error_msg=True)
                        else:
                            custom_print(f"[转存分享] 获取新文件夹文件列表失败，无法生成分享链接", error_msg=True)
                    else:
                        # 没有创建新文件夹，通过查询转存目标文件夹来获取转存后的文件
                        custom_print(f"[转存分享] 查询转存目标文件夹内的文件: {folder_id}")
                        file_list_data = await self.manager.get_sorted_file_list(
                            pdir_fid=folder_id, page='1', size='100', 
                            fetch_total='false', sort='file_type:asc,updated_at:desc'
                        )
                        
                        if file_list_data.get('code') == 0 and file_list_data.get('data', {}).get('list'):
                            # 获取转存的文件名列表
                            saved_files = result.get('files_list', [])
                            saved_folders = result.get('folders_list', [])
                            
                            # 在文件列表中查找匹配的文件/文件夹（按名称匹配，取最新的）
                            matched_items = []
                            for item in file_list_data['data']['list']:
                                item_name = item.get('file_name', '')
                                # 检查是否是刚转存的文件（在转存列表中）
                                if item_name in saved_files or item_name in saved_folders:
                                    matched_items.append(item)
                            
                            if matched_items:
                                # 使用第一个匹配的文件/文件夹生成分享链接
                                first_item = matched_items[0]
                                first_fid = first_item['fid']
                                first_file_name = first_item['file_name']
                                is_folder = first_item.get('dir', False) or first_item.get('file_type') == 0
                                
                                custom_print(f"[转存分享] 找到转存的文件: {first_file_name} (ID: {first_fid})")
                                share_url_new, title = await self.manager.create_share_link(
                                    first_fid, first_file_name, expired_type=1, password='', ad_fid=self.ad_fid
                                )
                                icon = "📁" if is_folder else "📄"
                                share_info.append({
                                    'title': title,
                                    'url': share_url_new,
                                    'icon': icon
                                })
                                custom_print(f"[转存分享] 成功生成分享链接: {share_url_new}")
                            else:
                                custom_print(f"[转存分享] 未找到转存的文件", error_msg=True)
                        else:
                            custom_print(f"[转存分享] 获取文件夹文件列表失败，无法生成分享链接", error_msg=True)
                except Exception as e:
                    custom_print(f"[转存分享] 生成分享链接失败: {str(e)}", error_msg=True)
                    import traceback
                    custom_print(traceback.format_exc(), error_msg=True)
            
            # 构建返回消息
            if generate_share:
                # 转存分享模式
                if share_info:
                    # 成功生成分享链接：发送两条消息
                    self.app.send_success("转存完成", "✅ 转存完成！", touser=touser)
                    share_item = share_info[0]
                    # 如果有原始文本，保留原文章结构并替换链接
                    if original_text:
                        result_text = original_text.replace(share_url, share_item['url'], 1)
                        self.app.send_text_message(result_text, touser=touser)
                    else:
                        # 如果没有原始文本，发送格式化链接
                        share_msg = f"{share_item['icon']} {share_item['title']}\n\n🔗 {share_item['url']}"
                        self.app.send_success("分享链接", share_msg, touser=touser)
                else:
                    # 转存成功但生成分享链接失败：只发送转存完成消息
                    self.app.send_success("转存完成", "✅ 转存完成！\n\n⚠️ 生成分享链接失败，但文件已成功转存", touser=touser)
                
                # 无论是否成功生成分享链接，都自动退出转存分享模式
                self.user_transfer_share_mode[user_key] = False
                custom_print(f"用户 {user_key} 已退出转存分享模式（本次任务完成）")
            else:
                # 只转存模式：只发送一条简洁消息
                self.app.send_success("转存完成", "✅ 转存完成！", touser=touser)
            
            return {
                'success': True,
                'message': '转存成功',
                'result': result,
                'share_links': [s['url'] for s in share_info]
            }
            
        except Exception as e:
            error_msg = f"处理分享链接失败：{str(e)}"
            self.app.send_error("处理失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
    
    async def _search_files_recursive(self, folder_id: str, keyword: str, current_path: str = "", 
                                      visited_folders: set = None, max_depth: int = 10, current_depth: int = 0) -> tuple[list, list]:
        """
        递归搜索文件夹及其子文件夹中的文件
        
        Args:
            folder_id: 文件夹ID
            keyword: 搜索关键词
            current_path: 当前路径（用于显示文件位置）
            visited_folders: 已访问的文件夹集合（防止循环）
            max_depth: 最大搜索深度
            current_depth: 当前深度
        
        Returns:
            tuple: (文件列表, 文件夹列表)
        """
        if visited_folders is None:
            visited_folders = set()
        
        # 防止循环和过深递归
        if folder_id in visited_folders or current_depth >= max_depth:
            return [], []
        
        visited_folders.add(folder_id)
        files = []
        folders = []
        
        try:
            # 获取当前文件夹中的文件列表
            file_list_data = await self.manager.get_sorted_file_list(
                pdir_fid=folder_id, page='1', size='100', 
                fetch_total='false', sort='file_name:asc'
            )
            
            if file_list_data.get('code') != 0:
                return files, folders
            
            data = file_list_data.get('data', {})
            file_list = data.get('list', []) if isinstance(data, dict) else []
            
            # 在当前文件夹中搜索匹配的文件
            for item in file_list:
                file_name = item.get('file_name', '')
                is_dir = item.get('dir') or item.get('file_type') == 0
                
                # 检查文件名是否匹配关键词
                if keyword.lower() in file_name.lower():
                    # 构建完整路径
                    full_path = f"{current_path}/{file_name}" if current_path else file_name
                    if is_dir:
                        folders.append({
                            'fid': item['fid'],
                            'name': file_name,
                            'type': '文件夹',
                            'path': current_path if current_path else "根目录"
                        })
                    else:
                        files.append({
                            'fid': item['fid'],
                            'name': file_name,
                            'type': '文件',
                            'size': item.get('size', 0),
                            'path': current_path if current_path else "根目录"
                        })
                
                # 如果是文件夹，递归搜索子文件夹
                if is_dir:
                    sub_folder_id = item['fid']
                    sub_folder_name = file_name
                    sub_path = f"{current_path}/{sub_folder_name}" if current_path else sub_folder_name
                    
                    # 递归搜索子文件夹
                    sub_files, sub_folders = await self._search_files_recursive(
                        sub_folder_id, keyword, sub_path, visited_folders, max_depth, current_depth + 1
                    )
                    files.extend(sub_files)
                    folders.extend(sub_folders)
        
        except Exception as e:
            custom_print(f"[搜索] 搜索文件夹 {folder_id} 时出错: {str(e)}", error_msg=True)
        
        return files, folders
    
    async def search_files(self, folder_id: str, keyword: str, touser: Optional[str] = None) -> dict:
        """
        搜索文件夹内的文件（递归搜索子文件夹）
        
        Args:
            folder_id: 文件夹ID，默认为'0'（根目录）
            keyword: 搜索关键词
            touser: 接收消息的用户ID
        
        Returns:
            dict: 搜索结果
        """
        if not self.manager:
            error_msg = "文件管理器未初始化，请先设置Cookie"
            self.app.send_error("搜索失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
        
        try:
            # 验证Cookie是否有效
            is_valid, user_info = await self.manager.verify_cookies()
            if not is_valid:
                error_msg = f"Cookie已失效，请重新设置Cookie\n{user_info}"
                self.app.send_warning("Cookie失效", error_msg, touser=touser)
                return {'success': False, 'message': error_msg, 'cookie_expired': True}
            
            # 发送搜索开始消息（不使用send_info，避免时间戳问题）
            search_start_msg = f"🔍 开始搜索（递归搜索子文件夹）\n\n正在搜索文件夹（ID: {folder_id}）及其子文件夹中的文件...\n关键词：{keyword}"
            self.app.send_text_message(search_start_msg, touser=touser)
            
            # 递归搜索所有子文件夹
            custom_print(f"[搜索] 开始递归搜索 - 文件夹ID: {folder_id}, 关键词: {keyword}")
            # 获取根文件夹名称（用于显示路径）
            root_folder_name = "根目录"
            if folder_id != '0':
                try:
                    root_list = await self.manager.get_sorted_file_list(
                        pdir_fid=folder_id, page='1', size='1', 
                        fetch_total='false', sort='file_name:asc'
                    )
                    # 这里无法直接获取文件夹名称，使用ID作为路径标识
                    root_folder_name = f"文件夹({folder_id[:8]}...)"
                except:
                    root_folder_name = "根目录"
            
            files, folders = await self._search_files_recursive(folder_id, keyword, root_folder_name)
            
            all_items = files + folders
            total = len(all_items)
            
            custom_print(f"[搜索] 搜索完成 - 匹配项: {total} (文件: {len(files)}, 文件夹: {len(folders)})")
            
            if total == 0:
                self.app.send_warning("搜索结果", f"未找到包含关键词 '{keyword}' 的文件或文件夹", touser=touser)
                return {
                    'success': True,
                    'keyword': keyword,
                    'folder_id': folder_id,
                    'items': [],
                    'total': 0
                }
            
            # 保存搜索结果（按用户ID保存，保存所有结果）
            user_key = touser if touser else 'default'
            self.user_search_results[user_key] = {
                'keyword': keyword,
                'folder_id': folder_id,
                'items': all_items,  # 保存所有结果
                'total': total,
                'files_count': len(files),
                'folders_count': len(folders),
                'current_page': 1  # 初始页码为1
            }
            
            # 显示第一页搜索结果
            await self._display_search_results_page(user_key, touser=touser)
            
            return {
                'success': True,
                'keyword': keyword,
                'folder_id': folder_id,
                'items': all_items[:20],
                'total': total,
                'files_count': len(files),
                'folders_count': len(folders)
            }
                
        except Exception as e:
            error_msg = f"搜索文件失败：{str(e)}"
            custom_print(error_msg, error_msg=True)
            import traceback
            custom_print(traceback.format_exc(), error_msg=True)
            self.app.send_error("搜索失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
    
    async def _display_search_results_page(self, user_key: str, page: int = None, touser: Optional[str] = None) -> None:
        """
        显示指定页码的搜索结果
        
        Args:
            user_key: 用户标识
            page: 页码（从1开始），如果为None则使用当前页码
            touser: 接收消息的用户ID
        """
        search_result = self.user_search_results.get(user_key)
        if not search_result:
            return
        
        items = search_result.get('items', [])
        total = search_result.get('total', 0)
        files_count = search_result.get('files_count', 0)
        folders_count = search_result.get('folders_count', 0)
        
        if not items or total == 0:
            return
        
        # 每页显示7个结果（因为还有标题和翻页提示，总共最多8个article）
        items_per_page = 7
        total_pages = (total + items_per_page - 1) // items_per_page  # 向上取整
        
        # 确定要显示的页码
        if page is None:
            current_page = search_result.get('current_page', 1)
        else:
            current_page = page
            search_result['current_page'] = current_page
        
        # 确保页码在有效范围内
        if current_page < 1:
            current_page = 1
        elif current_page > total_pages:
            current_page = total_pages
            search_result['current_page'] = current_page
        
        # 计算当前页的起始和结束索引
        start_idx = (current_page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total)
        display_items = items[start_idx:end_idx]
        
        articles = []
        # 第一个卡片显示总体信息和页码
        summary_title = f"✅ 找到 {total} 个匹配项（第 {current_page}/{total_pages} 页）"
        summary_desc = f"文件：{files_count} 个，文件夹：{folders_count} 个\n回复序号（{start_idx + 1}-{end_idx}）生成分享链接"
        if total_pages > 1:
            summary_desc += f"\n输入 'n' 下一页，'p' 上一页"
        
        articles.append({
            "title": summary_title,
            "description": summary_desc,
            "picurl": "",
            "url": ""
        })
        
        # 添加当前页的搜索结果项
        for idx, item in enumerate(display_items, start_idx + 1):
            # 构建标题：序号 + 类型 + 名称
            title = f"{idx}. [{item['type']}] {item['name']}"
            # 描述信息（显示路径和大小）
            description = f"类型：{item['type']}"
            if item.get('path'):
                description += f"\n路径：{item['path']}"
            if item.get('size'):
                # 格式化文件大小
                size = item['size']
                if size < 1024:
                    size_str = f"{size} B"
                elif size < 1024 * 1024:
                    size_str = f"{size / 1024:.2f} KB"
                elif size < 1024 * 1024 * 1024:
                    size_str = f"{size / (1024 * 1024):.2f} MB"
                else:
                    size_str = f"{size / (1024 * 1024 * 1024):.2f} GB"
                description += f"\n大小：{size_str}"
            
            articles.append({
                "title": title,
                "description": description,
                "picurl": "",
                "url": ""
            })
        
        # 发送卡片式消息
        self.app.send_news_message(articles, touser=touser)
    
    async def create_share_from_search(self, index: int, touser: Optional[str] = None) -> dict:
        """
        从搜索结果中为指定索引的文件生成分享链接
        
        Args:
            index: 文件索引（从1开始）
            touser: 接收消息的用户ID
        
        Returns:
            dict: 处理结果
        """
        if not self.manager:
            error_msg = "文件管理器未初始化，请先设置Cookie"
            self.app.send_error("生成失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
        
        # 获取用户的搜索结果
        user_key = touser if touser else 'default'
        last_search_result = self.user_search_results.get(user_key)
        
        if not last_search_result:
            error_msg = "没有可用的搜索结果，请先搜索文件"
            self.app.send_error("生成失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
        
        try:
            items = last_search_result.get('items', [])
            
            if not items:
                error_msg = "搜索结果为空"
                self.app.send_error("生成失败", error_msg, touser=touser)
                return {'success': False, 'message': error_msg}
            
            if index < 1 or index > len(items):
                error_msg = f"索引无效，请输入1-{len(items)}之间的数字"
                self.app.send_error("生成失败", error_msg, touser=touser)
                return {'success': False, 'message': error_msg}
            
            selected_item = items[index - 1]
            
            self.app.send_info("开始生成", f"正在为 {selected_item['type']} 生成分享链接...\n名称：{selected_item['name']}", touser=touser)
            
            # 生成分享链接
            share_url, title = await self.manager.create_share_link(
                selected_item['fid'], selected_item['name'], 
                expired_type=1, password='', ad_fid=self.ad_fid
            )
            
            # 构建简洁美观的消息格式
            type_icon = "📁" if selected_item['type'] == '文件夹' else "📄"
            result_msg = f"{type_icon} {title}\n\n"
            result_msg += f"🔗 {share_url}"
            
            self.app.send_success("分享链接", result_msg, touser=touser)
            
            return {
                'success': True,
                'message': '分享链接生成成功',
                'share_url': share_url,
                'title': title,
                'item': selected_item
            }
            
        except Exception as e:
            error_msg = f"生成分享链接失败：{str(e)}"
            custom_print(error_msg, error_msg=True)
            self.app.send_error("生成失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}
    
    async def verify_cookie(self, touser: Optional[str] = None) -> dict:
        """验证当前Cookie是否有效"""
        if not self.manager:
            self.app.send_error("验证失败", "文件管理器未初始化", touser=touser)
            return {'success': False, 'message': '文件管理器未初始化'}
        
        try:
            is_valid, user_info = await self.manager.verify_cookies()
            if is_valid:
                self.app.send_success("Cookie验证", f"Cookie有效\n用户：{user_info}", touser=touser)
                return {'success': True, 'message': f'Cookie有效，用户：{user_info}'}
            else:
                self.app.send_warning("Cookie失效", user_info, touser=touser)
                return {'success': False, 'message': user_info, 'cookie_expired': True}
        except Exception as e:
            error_msg = f"验证Cookie失败：{str(e)}"
            self.app.send_error("验证失败", error_msg, touser=touser)
            return {'success': False, 'message': error_msg}


def decrypt_echostr(encoding_aes_key: str, echostr: str, corp_id: str) -> str:
    """
    解密企业微信的echostr（使用EncodingAESKey）
    
    根据企业微信文档，解密流程：
    1. Base64解码EncodingAESKey并添加'='补齐
    2. Base64解码echostr
    3. 提取随机数（前16字节）和密文
    4. 使用AES-256-CBC解密
    5. 去除PKCS7填充
    6. 提取消息内容（格式：msg_len(4字节) + msg + corp_id）
    7. 验证corp_id
    8. 返回解密后的消息
    
    Args:
        encoding_aes_key: EncodingAESKey（43位字符）
        echostr: 加密的echostr（Base64编码）
        corp_id: 企业ID（用于验证）
    
    Returns:
        解密后的echostr
    
    Raises:
        ImportError: 如果pycryptodome未安装
        ValueError: 如果解密失败或corp_id不匹配
    """
    if not AES_AVAILABLE:
        raise ImportError("pycryptodome未安装，无法进行AES解密。请运行: pip install pycryptodome")
    
    try:
        # 1. Base64解码EncodingAESKey并添加'='补齐（43位字符补齐到44位）
        aes_key = base64.b64decode(encoding_aes_key + '=')
        if len(aes_key) != 32:
            raise ValueError(f"EncodingAESKey长度不正确: {len(aes_key)} 字节（应为32字节）")
        
        # IV是EncodingAESKey的前16字节
        iv = aes_key[:16]
        
        # 2. Base64解码echostr
        encrypted_msg = base64.b64decode(echostr)
        
        # 3. 使用AES-256-CBC解密（IV是密钥的前16字节，不是数据的前16字节）
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted_msg)
        
        # 4. 去除PKCS7填充
        pad = decrypted[-1]
        if pad > 16 or pad < 1:
            raise ValueError(f"无效的填充值: {pad}")
        
        # 去除填充后的数据格式：随机16字节 + msg_len(4字节网络字节序) + msg + corp_id
        content = decrypted[:-pad]
        
        # 5. 去掉前16字节的随机数
        if len(content) < 16:
            raise ValueError(f"消息内容太短: {len(content)} 字节")
        
        # content格式：msg_len(4字节) + msg + corp_id
        content = content[16:]
        
        # 检查内容长度（至少要有4字节长度字段）
        if len(content) < 4:
            raise ValueError(f"消息内容太短: {len(content)} 字节")
        
        # 6. 提取消息内容（格式：msg_len(4字节) + msg + corp_id）
        # 读取消息长度（网络字节序，大端）
        # 添加调试信息
        len_bytes = content[:4]
        custom_print(f"解密调试: content长度={len(content)}, 长度字段hex={len_bytes.hex()}")
        
        # 尝试大端读取
        msg_len = int.from_bytes(len_bytes, byteorder='big')
        custom_print(f"大端读取msg_len={msg_len}")
        
        # 剩余内容应该是 msg + corp_id
        remaining = content[4:]
        custom_print(f"remaining长度={len(remaining)}")
        
        # 如果大端读取的长度不合理，尝试小端读取
        if msg_len > len(remaining) or msg_len < 0:
            msg_len_le = int.from_bytes(len_bytes, byteorder='little')
            custom_print(f"大端读取失败，尝试小端读取: msg_len_le={msg_len_le}", error_msg=True)
            if 0 <= msg_len_le <= len(remaining):
                msg_len = msg_len_le
                custom_print(f"使用小端读取的长度: {msg_len}")
            else:
                raise ValueError(f"消息长度解析失败: 大端={msg_len}, 小端={msg_len_le}, remaining={len(remaining)}")
        
        # 提取消息和corp_id
        msg = remaining[:msg_len].decode('utf-8')
        received_corp_id = remaining[msg_len:].decode('utf-8')
        
        # 7. 验证corp_id
        if received_corp_id != corp_id:
            raise ValueError(f"CorpId不匹配: 期望 {corp_id}, 实际 {received_corp_id}")
        
        return msg
        
    except Exception as e:
        raise ValueError(f"AES解密失败: {str(e)}")


def verify_signature(token: str, timestamp: str, nonce: str, echostr: str, msg_signature: str) -> bool:
    """
    验证企业微信回调签名
    
    Args:
        token: 自定义令牌
        timestamp: 时间戳
        nonce: 随机字符串
        echostr: 加密字符串（或明文）
        msg_signature: 消息签名
    
    Returns:
        是否验证通过
    """
    if not token:
        # 如果没有配置token，跳过签名验证（不推荐）
        return True
    
    # 将token、timestamp、nonce、echostr按字典序排序（注意：字符串排序，不是数字排序）
    # 企业微信要求按字典序排序，然后将排序后的参数拼接成字符串
    tmp_list = [token, timestamp, nonce, echostr]
    tmp_list.sort()  # 字典序排序
    tmp_str = ''.join(tmp_list)  # 拼接
    
    # SHA1加密
    signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()
    
    # 比较签名（不区分大小写，企业微信返回的签名是小写）
    result = signature.lower() == msg_signature.lower()
    
    if not result:
        custom_print(f"签名验证失败 - 计算得到的签名: {signature}, 期望的签名: {msg_signature}", error_msg=True)
        custom_print(f"签名计算使用的字符串: {tmp_str[:200]}...", error_msg=True)
        custom_print(f"排序前的参数: token={token[:20]}..., timestamp={timestamp}, nonce={nonce}, echostr={echostr[:50]}...", error_msg=True)
        custom_print(f"排序后的顺序: {', '.join([f'{i}: {tmp_list[i][:30]}...' if len(tmp_list[i]) > 30 else f'{i}: {tmp_list[i]}' for i in range(len(tmp_list))])}", error_msg=True)
    
    return result


def parse_wechat_message(text: str, has_search_result: bool = False, is_search_mode: bool = False) -> dict:
    """
    解析企业微信消息
    
    支持的格式：
    1. 设置Cookie: cookie: <cookie内容>
    2. 验证Cookie: verify
    3. 搜索模式: /search <关键词> 或 在搜索模式下直接输入关键词
    4. 选择序号: 数字（从搜索结果中选择，仅当has_search_result为True时）
    5. 转存模式（默认）: 包含夸克网盘链接的文本（单个或多个）
    
    Args:
        text: 消息文本
        has_search_result: 是否有可用的搜索结果（用于判断数字是否为序号）
        is_search_mode: 是否处于搜索模式（在搜索模式下，直接输入文本会被当作搜索关键词）
    
    Returns:
        dict: 解析结果
    """
    text = text.strip()
    
    # 设置Cookie
    if text.lower().startswith('cookie:'):
        cookie = text[7:].strip()
        return {'type': 'cookie', 'content': cookie}
    
    # 验证Cookie
    if text.lower() in ['verify', '验证', '检查cookie']:
        return {'type': 'verify', 'content': ''}
    
    # 帮助命令
    if text.lower() in ['/help', 'help', '/帮助', '帮助']:
        return {'type': 'help', 'content': ''}
    
    # 搜索模式: /search <关键词> 或 在搜索模式下直接输入关键词
    if text.lower().startswith('/search'):
        keyword = text[7:].strip()  # 去掉 "/search" 前缀
        if keyword:
            return {'type': 'search', 'content': keyword}
        else:
            return {'type': 'error', 'content': '搜索关键词不能为空，格式：/search <关键词>'}
    
    # 优先检查链接（即使处于搜索模式，链接也应该被优先识别）
    # 默认转存模式：检查是否有夸克网盘链接
    url_pattern = r'https?://pan\.quark\.cn/s/[^\s\)]+'
    urls = re.findall(url_pattern, text)
    if urls:
        # 如果包含链接，返回转存类型
        if len(urls) == 1:
            # 单个链接
            return {'type': 'url', 'content': urls[0]}
        else:
            # 多个链接
            return {'type': 'urls', 'content': text}
    
    # 检查翻页命令（仅当有搜索结果时，优先于数字检查）
    if has_search_result and text.lower() in ['n', 'next', '下一页']:
        return {'type': 'page_next', 'content': ''}
    if has_search_result and text.lower() in ['p', 'prev', 'previous', '上一页']:
        return {'type': 'page_prev', 'content': ''}
    
    # 检查是否是数字（仅当有搜索结果时，才作为序号处理）
    if text.isdigit() and has_search_result:
        return {'type': 'select', 'content': int(text)}
    
    # 如果处于搜索模式，直接输入文本会被当作搜索关键词
    if is_search_mode and text and not text.startswith('/'):
        # 排除一些特殊命令
        if text.lower() not in ['verify', '验证', '检查cookie', 'help', '/help', '/帮助', '帮助']:
            return {'type': 'search', 'content': text}
    
    # 如果没有链接，也没有其他匹配，可能是误输入
    return {'type': 'unknown', 'content': text}


class AppHTTPHandler(BaseHTTPRequestHandler):
    """HTTP请求处理器（企业微信应用）"""
    
    app_handler: Optional[QuarkAppHandler] = None
    token: str = ''
    encoding_aes_key: str = ''
    corp_id: str = ''  # 企业ID，用于AES解密验证
    processing_messages: set[str] = set()  # 正在处理的消息（用于防止重复处理）
    def do_GET(self):
        """处理GET请求（URL验证）"""
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query_params = parse_qs(parsed_path.query)
        
        # 企业微信URL验证
        if path == '/wechat/callback':
            msg_signature = query_params.get('msg_signature', [''])[0]
            timestamp = query_params.get('timestamp', [''])[0]
            nonce = query_params.get('nonce', [''])[0]
            echostr_raw = query_params.get('echostr', [''])[0]
            
            # URL解码echostr（企业微信会进行URL编码）
            echostr = unquote(echostr_raw) if echostr_raw else ''
            
            # 企业微信URL验证（必须有echostr参数）
            if echostr:
                # 如果配置了EncodingAESKey，需要验证msg_signature并解密echostr
                # 如果只配置了Token，可以验证签名（可选，当前简化处理）
                # 如果都没配置，直接返回echostr
                
                # 如果配置了EncodingAESKey，必须有msg_signature（当前版本暂不支持加密，需要完整实现）
                if self.encoding_aes_key and not msg_signature:
                    custom_print(f"URL验证失败：配置了EncodingAESKey但缺少msg_signature", error_msg=True)
                    self.send_response(400)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'Missing msg_signature')
                    return
                
                # 如果配置了Token和msg_signature，进行签名验证
                if self.token and msg_signature:
                    custom_print(f"开始签名验证 - token: {self.token[:10]}..., timestamp: {timestamp}, nonce: {nonce}, echostr: {echostr[:20]}..., msg_signature: {msg_signature}")
                    if not verify_signature(self.token, timestamp, nonce, echostr, msg_signature):
                        custom_print(f"URL验证失败：签名不匹配", error_msg=True)
                        custom_print(f"计算签名使用的参数: token={self.token[:10]}..., timestamp={timestamp}, nonce={nonce}, echostr={echostr[:50]}...", error_msg=True)
                        self.send_response(403)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'Signature verification failed')
                        return
                    custom_print(f"URL验证成功：签名验证通过")
                elif self.token and not msg_signature:
                    # 只配置了Token但没有msg_signature（明文模式），直接返回echostr
                    custom_print(f"URL验证请求（明文模式，跳过签名验证）")
                else:
                    # 未配置Token，直接返回echostr（不推荐，但允许）
                    custom_print(f"URL验证请求（未配置Token，跳过签名验证）")
                
                # 如果有EncodingAESKey，需要解密echostr
                if self.encoding_aes_key:
                    if not AES_AVAILABLE:
                        custom_print(f"错误：配置了EncodingAESKey但pycryptodome未安装", error_msg=True)
                        custom_print(f"请运行: pip install pycryptodome", error_msg=True)
                        self.send_response(500)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'pycryptodome not installed')
                        return
                    
                    if not self.corp_id:
                        custom_print(f"错误：配置了EncodingAESKey但未配置CorpId", error_msg=True)
                        self.send_response(500)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'CorpId not configured')
                        return
                    
                    try:
                        # 解密echostr
                        decrypted_echostr = decrypt_echostr(self.encoding_aes_key, echostr, self.corp_id)
                        custom_print(f"AES解密成功，返回解密后的echostr")
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(decrypted_echostr.encode('utf-8'))
                        return
                    except Exception as e:
                        custom_print(f"AES解密失败: {str(e)}", error_msg=True)
                        self.send_response(500)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(f'AES decryption failed: {str(e)}'.encode('utf-8'))
                        return
                else:
                    # 如果没有EncodingAESKey，echostr就是明文，直接返回
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(echostr.encode('utf-8'))
                    custom_print(f"URL验证成功，返回echostr（明文模式）")
                    return
            else:
                # 直接访问回调URL时返回提示信息
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                response = """<html><head><meta charset="utf-8"><title>企业微信回调接口</title></head><body>
<h2>企业微信应用回调接口</h2>
<p>此接口用于接收企业微信的消息推送。</p>
<p>请在企业微信管理后台配置此URL：<code>http://your-server:8888/wechat/callback</code></p>
<p>状态：✅ 服务运行正常</p>
</body></html>"""
                self.wfile.write(response.encode('utf-8'))
                custom_print(f"访问回调URL（非验证请求）")
                return
        
        if path == '/health':
            self._send_response(200, {'status': 'ok'})
        else:
            self._send_response(404, {'error': '接口不存在'})
    
    def do_POST(self):
        """处理POST请求（接收消息）"""
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path
            query_params = parse_qs(parsed_path.query)
            
            if path == '/wechat/callback':
                # 接收企业微信消息
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                post_data_str = post_data.decode('utf-8')
                
                try:
                    # 获取URL参数
                    msg_signature = query_params.get('msg_signature', [''])[0]
                    timestamp = query_params.get('timestamp', [''])[0]
                    nonce = query_params.get('nonce', [''])[0]
                    
                    xml_content = None
                    
                    # 如果配置了EncodingAESKey，需要解密消息
                    if self.encoding_aes_key and self.token and self.corp_id:
                        if not WXBIZ_MSG_CRYPT_AVAILABLE:
                            custom_print("错误: 配置了EncodingAESKey但WXBizMsgCrypt3不可用", error_msg=True)
                            self.send_response(500)
                            self.send_header('Content-Type', 'text/plain')
                            self.end_headers()
                            self.wfile.write(b'WXBizMsgCrypt3 not available')
                            return
                        
                        if not msg_signature or not timestamp or not nonce:
                            custom_print(f"错误: 加密模式下缺少必要参数: msg_signature={bool(msg_signature)}, timestamp={bool(timestamp)}, nonce={bool(nonce)}", error_msg=True)
                            self.send_response(400)
                            self.send_header('Content-Type', 'text/plain')
                            self.end_headers()
                            self.wfile.write(b'Missing required parameters')
                            return
                        
                        try:
                            # 使用WXBizMsgCrypt解密消息
                            # 清理corp_id，去除可能的空格和换行符
                            corp_id_clean = self.corp_id.strip() if self.corp_id else ''
                            custom_print(f"开始解密消息 - corp_id: '{corp_id_clean}' (长度: {len(corp_id_clean)})")
                            custom_print(f"EncodingAESKey长度: {len(self.encoding_aes_key) if self.encoding_aes_key else 0}")
                            
                            wxcpt = WXBizMsgCrypt(self.token, self.encoding_aes_key, corp_id_clean)
                            ret, xml_content = wxcpt.DecryptMsg(post_data_str, msg_signature, timestamp, nonce)
                            
                            if ret != 0:
                                custom_print(f"解密消息失败，错误码: {ret}", error_msg=True)
                                if ret == -40005:
                                    custom_print(f"企业ID验证失败！", error_msg=True)
                                    custom_print(f"配置的corp_id: '{corp_id_clean}' (长度: {len(corp_id_clean)})", error_msg=True)
                                    custom_print(f"提示: 请检查以下几点：", error_msg=True)
                                    custom_print(f"  1. 企业微信管理后台 -> 我的企业 -> 企业信息 -> 企业ID", error_msg=True)
                                    custom_print(f"  2. 确保配置文件中的corp_id与企业微信后台显示的企业ID完全一致（包括大小写）", error_msg=True)
                                    custom_print(f"  3. 检查是否有空格、换行符等隐藏字符", error_msg=True)
                                    custom_print(f"  4. 如果企业ID正确，可能是EncodingAESKey配置错误", error_msg=True)
                                self.send_response(200)  # 返回200避免企业微信重复推送
                                self.send_header('Content-Type', 'text/xml')
                                self.end_headers()
                                return
                            
                            custom_print(f"消息解密成功")
                            xml_content = xml_content.decode('utf-8')
                        except Exception as e:
                            custom_print(f"解密消息异常: {str(e)}", error_msg=True)
                            import traceback
                            custom_print(traceback.format_exc(), error_msg=True)
                            self.send_response(200)  # 返回200避免企业微信重复推送
                            self.send_header('Content-Type', 'text/xml')
                            self.end_headers()
                            return
                    else:
                        # 明文模式，直接解析XML
                        xml_content = post_data_str
                    
                    # 解析XML消息
                    root = ET.fromstring(xml_content)
                    msg_type = root.find('MsgType').text if root.find('MsgType') is not None else ''
                    content_elem = root.find('Content')
                    content = content_elem.text if content_elem is not None and content_elem.text else ''
                    from_user_elem = root.find('FromUserName')
                    from_user = from_user_elem.text if from_user_elem is not None and from_user_elem.text else ''
                    
                    custom_print(f"收到消息 - 类型: {msg_type}, 用户: {from_user}, 内容: {content[:50]}")
                    
                    # 处理事件消息（菜单点击）
                    if msg_type == 'event':
                        event_elem = root.find('Event')
                        event = event_elem.text if event_elem is not None else ''
                        event_key_elem = root.find('EventKey')
                        event_key = event_key_elem.text if event_key_elem is not None else ''
                        
                        custom_print(f"收到事件消息 - 事件类型: {event}, EventKey: {event_key}")
                        
                        # 处理菜单点击事件
                        if event == 'click':
                            # 先返回HTTP响应
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/xml')
                            self.end_headers()
                            self.wfile.write(b'<xml><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[]]></Content></xml>')
                            self.wfile.flush()
                            
                            # 处理菜单点击
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            loop.run_until_complete(self._handle_menu_click(event_key, from_user))
                            loop.close()
                            return
                    
                    # 只处理文本消息
                    if msg_type != 'text':
                        custom_print(f"忽略非文本消息: {msg_type}")
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/xml')
                        self.end_headers()
                        return
                    
                    # 简单的消息去重机制（防止企业微信重试导致重复处理）
                    import hashlib
                    message_hash = hashlib.md5(f"{from_user}:{content}".encode('utf-8')).hexdigest()
                    if message_hash in AppHTTPHandler.processing_messages:
                        custom_print(f"消息正在处理中，忽略重复请求: {content[:50]}...")
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/xml')
                        self.end_headers()
                        self.wfile.write(b'<xml><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[]]></Content></xml>')
                        return
                    
                    # 标记消息为正在处理
                    AppHTTPHandler.processing_messages.add(message_hash)
                    
                    # 先返回HTTP响应（避免企业微信超时重试导致重复推送）
                    # 企业微信要求在5秒内收到响应，否则会重试
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/xml')
                    self.end_headers()
                    self.wfile.write(b'<xml><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[]]></Content></xml>')
                    self.wfile.flush()  # 确保响应已发送
                    
                    # 然后在后台处理消息（响应已发送，企业微信不会再重试）
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(self._handle_message(content, from_user))
                        loop.close()
                    finally:
                        # 处理完成后，从处理列表中移除（延迟10秒，防止短时间内重复）
                        import threading
                        def remove_hash():
                            import time
                            time.sleep(10)
                            AppHTTPHandler.processing_messages.discard(message_hash)
                        threading.Thread(target=remove_hash, daemon=True).start()
                    
                except Exception as e:
                    custom_print(f"解析消息失败: {str(e)}", error_msg=True)
                    import traceback
                    custom_print(traceback.format_exc(), error_msg=True)
                    self.send_response(200)  # 即使失败也返回200
                    self.send_header('Content-Type', 'text/xml')
                    self.end_headers()
                    
            else:
                self._send_response(404, {'error': '接口不存在'})
        
        except Exception as e:
            custom_print(f"处理请求失败: {str(e)}", error_msg=True)
            import traceback
            custom_print(traceback.format_exc(), error_msg=True)
            self.send_response(200)  # 返回200避免企业微信重复推送
            self.end_headers()
    
    async def _handle_menu_click(self, event_key: str, from_user: str):
        """处理菜单点击事件"""
        if not self.app_handler:
            custom_print("app_handler未初始化", error_msg=True)
            return
        
        try:
            user_key = from_user if from_user else 'default'
            
            if event_key == '/transfer_share' or event_key == 'transfer_share':
                # 点击转存分享菜单，进入转存分享模式
                self.app_handler.user_transfer_share_mode[user_key] = True
                custom_print(f"用户 {user_key} 进入转存分享模式，当前状态: {self.app_handler.user_transfer_share_mode}")
                self.app_handler.app.send_info("转存分享模式", "✅ 已进入转存分享模式\n\n发送夸克网盘链接将自动转存并生成分享链接\n\n提示：发送链接后会同时转存和分享", touser=from_user)
            elif event_key == '/search' or event_key == 'search':
                # 点击搜索菜单，进入搜索模式
                self.app_handler.user_search_mode[user_key] = True
                self.app_handler.app.send_info("搜索模式", "🔍 已进入搜索模式\n\n请输入要搜索的关键词，例如：视频、电影、文档等\n\n提示：输入 /help 查看完整帮助", touser=from_user)
                custom_print(f"用户 {user_key} 进入搜索模式")
            elif event_key == '/help' or event_key == 'help':
                # 点击帮助菜单
                help_msg = """📖 **使用说明**

**1. 转存模式（默认）**
   • 直接发送夸克网盘分享链接即可自动转存
   • 支持单个链接：发送一个链接
   • 支持批量链接：发送多个链接（保留原文格式）
   • 默认只转存，不生成分享链接

**2. 转存分享模式**
   • 点击菜单栏"转存分享"按钮进入转存分享模式
   • 在此模式下，转存后会自动生成新的分享链接
   • 批量转存时会保留原文结构，并用新链接替换原链接

**3. 搜索模式**
   • 点击菜单栏"搜索"按钮进入搜索模式
   • 或使用命令：`/search <关键词>`
   • 在搜索模式下，直接输入关键词即可搜索
   • 搜索结果会显示列表，输入序号（1-20）即可生成分享链接

**4. 其他命令**
   • `cookie: <cookie内容>` - 设置Cookie
   • `verify` - 验证Cookie是否有效
   • `/help` - 显示此帮助信息

**使用示例：**
1. 转存单个文件：直接发送链接
2. 批量转存：发送包含多个链接的文本
3. 搜索文件：点击"搜索"菜单，然后输入关键词
4. 生成链接：搜索后输入数字 `1`、`2` 等"""
                self.app_handler.app.send_info("使用帮助", help_msg, touser=from_user)
                # 退出所有模式
                self.app_handler.user_search_mode[user_key] = False
                self.app_handler.user_transfer_share_mode[user_key] = False
            elif event_key == 'verify':
                # 点击验证菜单
                custom_print("执行：验证Cookie（来自菜单）")
                await self.app_handler.verify_cookie(touser=from_user)
            elif event_key == '/add_ban':
                # 添加屏蔽词
                self.app_handler.user_waiting_ban_input[user_key] = True
                current_ban = ",".join(self.app_handler.banned_keywords) if self.app_handler.banned_keywords else "无"
                self.app_handler.app.send_info(
                    "添加屏蔽词",
                    f"请输入屏蔽词，多个用英文逗号分隔，例如：词1,词2\n\n当前屏蔽词：{current_ban}",
                    touser=from_user
                )
                custom_print(f"用户 {user_key} 准备添加屏蔽词")
            elif event_key == '/scan_ban':
                # 手动扫描最近转存目录
                try:
                    result = await self.app_handler.manager.scan_recent_folders_for_banned()
                    folders = result.get("folders") or []
                    scanned = result.get("scanned") or 0
                    matched = result.get("matched") or 0
                    deleted = result.get("deleted") or 0
                    msg = "已执行屏蔽词扫描（最近转存目录）\n"
                    msg += f"扫描目录数：{len(folders)}\n"
                    msg += f"扫描文件/文件夹数：{scanned}\n"
                    msg += f"匹配屏蔽词：{matched}\n"
                    msg += f"删除数量：{deleted}\n"
                    if folders:
                        msg += "目录ID：\n" + "\n".join(folders[:10])
                        if len(folders) > 10:
                            msg += "\n..."
                    self.app_handler.app.send_success("扫描完成", msg, touser=from_user)
                except Exception as e:
                    self.app_handler.app.send_error("扫描失败", str(e), touser=from_user)
                custom_print(f"用户 {user_key} 手动触发屏蔽扫描")
            else:
                custom_print(f"未知的菜单事件: {event_key}")
        except Exception as e:
            custom_print(f"处理菜单点击失败: {str(e)}", error_msg=True)
            import traceback
            custom_print(traceback.format_exc(), error_msg=True)
    
    async def _handle_message(self, content: str, from_user: str):
        """处理用户消息（消息已经在do_POST中标记为已处理）"""
        if not self.app_handler:
            custom_print("app_handler未初始化", error_msg=True)
            return
        
        try:
            user_key = from_user if from_user else 'default'
            # 若等待输入屏蔽词，优先处理
            if self.app_handler.user_waiting_ban_input.get(user_key, False):
                keywords = [k.strip() for k in content.replace("，", ",").split(",") if k.strip()]
                if keywords:
                    self.app_handler._update_banned_keywords(keywords)
                    self.app_handler.app.send_success("添加屏蔽词成功", "已加入屏蔽词：" + ",".join(keywords), touser=from_user)
                else:
                    self.app_handler.app.send_warning("添加屏蔽词失败", "输入为空，请重新输入", touser=from_user)
                self.app_handler.user_waiting_ban_input[user_key] = False
                return

            # 检查用户是否有搜索结果和搜索模式状态
            has_search_result = user_key in self.app_handler.user_search_results
            is_search_mode = self.app_handler.user_search_mode.get(user_key, False)
            is_transfer_share_mode = self.app_handler.user_transfer_share_mode.get(user_key, False)
            custom_print(f"处理消息 - 用户: {user_key}, 内容: {content[:100]}")
            custom_print(f"  搜索模式: {is_search_mode}, 转存分享模式: {is_transfer_share_mode}, 有搜索结果: {has_search_result}")
            
            # 解析消息（传入搜索模式状态）
            parsed = parse_wechat_message(content, has_search_result=has_search_result, is_search_mode=is_search_mode)
            msg_type = parsed['type']
            msg_content = parsed['content']
            custom_print(f"消息解析结果 - 类型: {msg_type}, 内容: {str(msg_content)[:100]}")
            
            if msg_type == 'cookie':
                custom_print("执行：设置Cookie")
                await self.app_handler.set_cookie(msg_content, touser=from_user)
            elif msg_type == 'verify':
                custom_print("执行：验证Cookie")
                await self.app_handler.verify_cookie(touser=from_user)
            elif msg_type == 'help':
                custom_print("执行：显示帮助")
                # 显示帮助信息
                help_msg = """📖 **使用说明**

**1. 转存模式（默认）**
   • 直接发送夸克网盘分享链接即可自动转存
   • 支持单个链接：发送一个链接
   • 支持批量链接：发送多个链接（保留原文格式）
   • 默认只转存，不生成分享链接

**2. 转存分享模式**
   • 点击菜单栏"转存分享"按钮进入转存分享模式
   • 在此模式下，转存后会自动生成新的分享链接
   • 批量转存时会保留原文结构，并用新链接替换原链接

**3. 搜索模式**
   • 点击菜单栏"搜索"按钮进入搜索模式
   • 或使用命令：`/search <关键词>`
   • 在搜索模式下，直接输入关键词即可搜索
   • 搜索结果会显示列表，输入序号（1-20）即可生成分享链接

**4. 其他命令**
   • `cookie: <cookie内容>` - 设置Cookie
   • `verify` - 验证Cookie是否有效
   • `/help` - 显示此帮助信息

**使用示例：**
1. 转存单个文件：直接发送链接
2. 批量转存：发送包含多个链接的文本
3. 搜索文件：点击"搜索"菜单，然后输入关键词
4. 生成链接：搜索后输入数字 `1`、`2` 等"""
                self.app_handler.app.send_info("使用帮助", help_msg, touser=from_user)
            elif msg_type == 'search':
                # 搜索模式：/search <关键词> 或 在搜索模式下直接输入关键词
                custom_print(f"执行：搜索文件 - 关键词: {msg_content}")
                folder_id = self.app_handler.search_folder_id
                # 执行搜索后，自动退出搜索模式
                await self.app_handler.search_files(folder_id, msg_content, touser=from_user)
                self.app_handler.user_search_mode[user_key] = False
            elif msg_type == 'page_next':
                # 翻到下一页
                custom_print(f"执行：翻到下一页")
                search_result = self.app_handler.user_search_results.get(user_key)
                if search_result:
                    current_page = search_result.get('current_page', 1)
                    items = search_result.get('items', [])
                    items_per_page = 7
                    total_pages = (len(items) + items_per_page - 1) // items_per_page
                    if current_page < total_pages:
                        await self.app_handler._display_search_results_page(user_key, current_page + 1, touser=from_user)
                    else:
                        self.app_handler.app.send_info("提示", "已经是最后一页了", touser=from_user)
                else:
                    self.app_handler.app.send_error("错误", "没有可用的搜索结果", touser=from_user)
            elif msg_type == 'page_prev':
                # 翻到上一页
                custom_print(f"执行：翻到上一页")
                search_result = self.app_handler.user_search_results.get(user_key)
                if search_result:
                    current_page = search_result.get('current_page', 1)
                    if current_page > 1:
                        await self.app_handler._display_search_results_page(user_key, current_page - 1, touser=from_user)
                    else:
                        self.app_handler.app.send_info("提示", "已经是第一页了", touser=from_user)
                else:
                    self.app_handler.app.send_error("错误", "没有可用的搜索结果", touser=from_user)
            elif msg_type == 'select':
                # 从搜索结果中选择序号
                custom_print(f"执行：选择序号 - 序号: {msg_content}")
                # 选择序号后，自动退出搜索模式（保留搜索结果，用户再次输入数字仍可选择）
                await self.app_handler.create_share_from_search(msg_content, touser=from_user)
                self.app_handler.user_search_mode[user_key] = False
            elif msg_type == 'url':
                # 单个链接转存
                custom_print(f"执行：转存单个链接 - URL: {msg_content[:50]}")
                # 转存链接时，退出搜索模式
                self.app_handler.user_search_mode[user_key] = False
                # 传递原始文本以保留原文章结构（在转存分享模式下）
                await self.app_handler.process_share_url(msg_content, original_text=content, touser=from_user)
            elif msg_type == 'urls':
                # 多个链接批量转存
                custom_print(f"执行：批量转存链接 - 内容长度: {len(msg_content)}")
                # 转存链接时，退出搜索模式
                self.app_handler.user_search_mode[user_key] = False
                await self.app_handler.process_text_with_links(msg_content, touser=from_user)
            elif msg_type == 'error':
                # 错误消息
                custom_print(f"输入错误: {msg_content}")
                self.app_handler.app.send_error("输入错误", msg_content, touser=from_user)
            else:
                # 未知消息类型（可能是误输入，提示用户）
                custom_print(f"未知消息类型: {msg_type}, 内容: {content[:50]}")
                # 如果处于搜索模式，提示用户输入关键词
                if is_search_mode:
                    self.app_handler.app.send_info("提示", 
                        "🔍 您当前处于搜索模式\n\n请输入要搜索的关键词，例如：视频、电影、文档等\n\n提示：发送链接可退出搜索模式并转存文件", 
                        touser=from_user)
                else:
                    self.app_handler.app.send_info("提示", 
                        "请输入以下格式之一：\n"
                        "1. 夸克网盘链接（单个或多个）- 自动转存\n"
                        "2. 点击菜单栏「搜索」按钮或输入 /search <关键词> - 搜索文件\n"
                        "3. 数字（在搜索后输入）- 选择序号生成链接\n"
                        "4. /help - 查看帮助", 
                        touser=from_user)
        except Exception as e:
            custom_print(f"处理消息失败: {str(e)}", error_msg=True)
            import traceback
            custom_print(traceback.format_exc(), error_msg=True)
            self.app_handler.app.send_error("处理失败", f"处理消息时发生错误：{str(e)}", touser=from_user)
    
    def _send_response(self, status_code: int, data: dict):
        """发送响应"""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def log_message(self, format, *args):
        """自定义日志输出"""
        custom_print(f"{self.address_string()} - {format % args}")


class WeChatAppServer:
    """企业微信应用服务器"""
    
    def __init__(self, corp_id: str, agent_id: str, secret: str, 
                 host: str = '0.0.0.0', port: int = 8888, 
                 default_folder_id: str = '0', search_folder_id: str = '0',
                 token: str = '', encoding_aes_key: str = '',
                 proxy: Optional[str] = None,
                 banned_keywords: Optional[list[str]] = None,
                 ad_fid: str = ''):
        """
        初始化服务器
        
        Args:
            corp_id: 企业ID
            agent_id: 应用ID
            secret: 应用密钥
            host: 服务器监听地址
            port: 服务器监听端口
            default_folder_id: 默认保存文件夹ID
            search_folder_id: 默认搜索文件夹ID
            token: 自定义令牌（用于URL验证）
            encoding_aes_key: 消息加密密钥（43位）
            proxy: 微信API代理地址（可选，默认：https://qyapi.weixin.qq.com）
                   2022年6月20日后创建的自建应用才需要配置代理
                   不使用代理时需要保留默认值'https://qyapi.weixin.qq.com'
        """
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.host = host
        self.port = port
        self.app_handler = QuarkAppHandler(
            corp_id, agent_id, secret, default_folder_id, search_folder_id,
            proxy=proxy, banned_keywords=banned_keywords, ad_fid=ad_fid
        )
        AppHTTPHandler.app_handler = self.app_handler
        AppHTTPHandler.token = token
        AppHTTPHandler.encoding_aes_key = encoding_aes_key
        AppHTTPHandler.corp_id = corp_id  # 设置企业ID，用于AES解密验证
    
    def create_default_menu(self):
        """创建默认菜单"""
        try:
            buttons = [
                {
                    "name": "转存分享",
                    "type": "click",
                    "key": "/transfer_share"
                },
                {
                    "name": "搜索",
                    "type": "click",
                    "key": "/search"
                },
                {
                    "name": "帮助",
                    "sub_button": [
                        {
                            "type": "click",
                            "name": "使用说明",
                            "key": "/help"
                        },
                        {
                            "type": "click",
                            "name": "验证Cookie",
                            "key": "verify"
                        },
                        {
                            "type": "click",
                            "name": "添加屏蔽词",
                            "key": "/add_ban"
                        },
                        {
                            "type": "click",
                            "name": "手动扫描屏蔽",
                            "key": "/scan_ban"
                        }
                    ]
                }
            ]
            if self.app_handler.app.create_menu(buttons):
                custom_print("默认菜单创建成功")
            else:
                custom_print("默认菜单创建失败", error_msg=True)
        except Exception as e:
            custom_print(f"创建默认菜单异常: {str(e)}", error_msg=True)
    
    def start(self, create_menu: bool = False):
        """
        启动服务器
        
        Args:
            create_menu: 是否在启动时创建默认菜单
        """
        server = HTTPServer((self.host, self.port), AppHTTPHandler)
        custom_print(f"企业微信应用服务器启动成功")
        custom_print(f"监听地址: http://{self.host}:{self.port}")
        custom_print(f"回调URL: http://{self.host}:{self.port}/wechat/callback")
        custom_print(f"可用接口:")
        custom_print(f"  GET/POST /wechat/callback - 接收企业微信消息")
        custom_print(f"  GET /health - 健康检查")
        
        # 如果指定创建菜单，则创建默认菜单
        if create_menu:
            self.create_default_menu()
        
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            custom_print("服务器已停止")
            server.shutdown()


if __name__ == '__main__':
    import sys
    
    # 立即输出启动信息，确保调试信息能显示
    sys.stdout.flush()
    print("=" * 60, flush=True)
    print("[启动] QuarkPanTool 程序开始启动...", flush=True)
    print("=" * 60, flush=True)
    sys.stdout.flush()
    
    # 从配置文件读取配置
    try:
        config = read_config(os.path.join(CONFIG_DIR, 'bot_config.json'), 'json')
        corp_id = config.get('corp_id', '').strip()  # 清理空格和换行符
        agent_id = config.get('agent_id', '')
        secret = config.get('secret', '')
        token = config.get('token', '')
        encoding_aes_key = config.get('encoding_aes_key', '')
        host = config.get('host', '0.0.0.0')
        port = config.get('port', 8888)
        default_folder_id = config.get('default_folder_id', '0')
        search_folder_id = config.get('search_folder_id', '0')
        # 微信API代理地址（2022年6月20日后创建的自建应用才需要）
        # 不使用代理时需要保留默认值'https://qyapi.weixin.qq.com'
        proxy = config.get('proxy', 'https://qyapi.weixin.qq.com')
        quark_banned = config.get('quark_banned', '')
        ad_fid = config.get('ad_fid', '')
        banned_list = [k.strip() for k in quark_banned.split(',') if k.strip()] if isinstance(quark_banned, str) else []
        
        # 输出配置信息（隐藏敏感信息）
        custom_print(f"配置信息:")
        custom_print(f"  企业ID (corp_id): '{corp_id}' (长度: {len(corp_id)})")
        custom_print(f"  应用ID (agent_id): {agent_id}")
        custom_print(f"  应用密钥 (secret): {'*' * min(10, len(secret))}... (长度: {len(secret)})")
        custom_print(f"  Token: {'已配置' if token else '未配置'} (长度: {len(token) if token else 0})")
        custom_print(f"  EncodingAESKey: {'已配置' if encoding_aes_key else '未配置'} (长度: {len(encoding_aes_key) if encoding_aes_key else 0})")
        custom_print(f"  代理地址: {proxy}")
        custom_print(f"  广告过滤关键词: {quark_banned if quark_banned else '未配置'}")
        custom_print(f"  ad_fid: {ad_fid if ad_fid else '未配置'}")
    except Exception as e:
        custom_print(f"读取配置失败: {str(e)}", error_msg=True)
        sys.exit(1)
    
    if not corp_id or not agent_id or not secret:
        print("错误：未配置企业微信应用信息")
        print("请在 config/bot_config.json 中配置 corp_id、agent_id 和 secret")
        sys.exit(1)
    
    # 启动服务器
    server = WeChatAppServer(
        corp_id, agent_id, secret, host, port, 
        default_folder_id, search_folder_id, token, encoding_aes_key, proxy,
        banned_keywords=banned_list, ad_fid=ad_fid
    )
    server.start(create_menu=True)


