"""
企业微信应用API模块（简化版）
支持企业微信应用的消息发送（通过AccessToken）
注意：消息加密功能需要pycryptodome库，如果不需要接收加密消息可以省略
"""
import json
import time
import httpx
from typing import Optional
from utils import get_datetime


def adapt_request_url(base_url: str, path: str) -> str:
    """
    适配请求URL（用于代理支持）
    
    Args:
        base_url: 基础URL（如：https://qyapi.weixin.qq.com 或代理地址）
        path: 相对路径（如：cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}）
    
    Returns:
        完整的URL
    """
    # 移除base_url末尾的斜杠
    base_url = base_url.rstrip('/')
    # 确保path以/开头
    if not path.startswith('/'):
        path = '/' + path
    return base_url + path


class WeChatApp:
    """企业微信应用类（使用AccessToken发送消息）"""
    
    def __init__(self, corp_id: str, agent_id: str, secret: str, proxy: Optional[str] = None):
        """
        初始化企业微信应用
        
        Args:
            corp_id: 企业ID
            agent_id: 应用ID
            secret: 应用密钥（用于获取AccessToken）
            proxy: 代理服务器地址（可选，默认：https://qyapi.weixin.qq.com）
                   2022年6月20日后创建的自建应用才需要配置代理
                   不使用代理时需要保留默认值'https://qyapi.weixin.qq.com'
        """
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self._access_token = None
        self._token_expires_at = 0
        # 设置代理地址，默认为企业微信官方API地址
        self._proxy = proxy or "https://qyapi.weixin.qq.com"
        
        # 构建API URL（使用代理适配）
        self._token_url = adapt_request_url(self._proxy, "cgi-bin/gettoken")
        self._send_msg_url = adapt_request_url(self._proxy, "cgi-bin/message/send")
        self._create_menu_url = adapt_request_url(self._proxy, "cgi-bin/menu/create")
        self._delete_menu_url = adapt_request_url(self._proxy, "cgi-bin/menu/delete")
    
    def get_access_token(self, force_refresh: bool = False) -> str:
        """
        获取AccessToken（会自动缓存和刷新）
        
        Args:
            force_refresh: 是否强制刷新
        
        Returns:
            AccessToken字符串
        """
        # 如果token未过期且不强制刷新，直接返回缓存的token
        if not force_refresh and self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        
        try:
            url = self._token_url
            params = {
                'corpid': self.corp_id,
                'corpsecret': self.secret
            }
            
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url, params=params)
                result = response.json()
                
                if result.get("errcode") == 0:
                    self._access_token = result.get("access_token")
                    # Token有效期通常是7200秒，提前5分钟刷新
                    expires_in = result.get("expires_in", 7200)
                    self._token_expires_at = time.time() + expires_in - 300
                    return self._access_token
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    raise Exception(f"获取AccessToken失败: {error_msg} (错误码: {result.get('errcode')})")
        except Exception as e:
            raise Exception(f"获取AccessToken异常: {str(e)}")
    
    def send_text_message(self, content: str, touser: Optional[str] = None,
                         toparty: Optional[str] = None, totag: Optional[str] = None,
                         safe: int = 0) -> bool:
        """
        发送文本消息
        
        Args:
            content: 消息内容
            touser: 用户ID列表，多个用|分隔，@all表示所有成员
            toparty: 部门ID列表，多个用|分隔
            totag: 标签ID列表，多个用|分隔
            safe: 是否保密消息，0表示可对外分享，1表示不能分享且内容显示水印
        
        Returns:
            是否发送成功
        """
        access_token = self.get_access_token()
        
        url = f"{self._send_msg_url}?access_token={access_token}"
        
        data = {
            "touser": touser or "@all",
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {
                "content": content
            },
            "safe": safe
        }
        
        if toparty:
            data["toparty"] = toparty
        if totag:
            data["totag"] = totag
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=data)
                result = response.json()
                
                if result.get("errcode") == 0:
                    return True
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    print(f"发送消息失败: {error_msg} (错误码: {result.get('errcode')})")
                    return False
        except Exception as e:
            print(f"发送消息异常: {str(e)}")
            return False
    
    def send_markdown_message(self, content: str, touser: Optional[str] = None,
                              toparty: Optional[str] = None, totag: Optional[str] = None) -> bool:
        """
        发送Markdown消息
        
        Args:
            content: Markdown格式的消息内容
            touser: 用户ID列表
            toparty: 部门ID列表
            totag: 标签ID列表
        
        Returns:
            是否发送成功
        """
        access_token = self.get_access_token()
        
        url = f"{self._send_msg_url}?access_token={access_token}"
        
        data = {
            "touser": touser or "@all",
            "msgtype": "markdown",
            "agentid": self.agent_id,
            "markdown": {
                "content": content
            }
        }
        
        if toparty:
            data["toparty"] = toparty
        if totag:
            data["totag"] = totag
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=data)
                result = response.json()
                
                if result.get("errcode") == 0:
                    return True
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    print(f"发送Markdown消息失败: {error_msg} (错误码: {result.get('errcode')})")
                    return False
        except Exception as e:
            print(f"发送Markdown消息异常: {str(e)}")
            return False
    
    def send_success(self, title: str, content: str, touser: Optional[str] = None) -> bool:
        """发送成功消息（使用text类型，支持普通微信）"""
        text_content = f"✅ {title}\n\n"
        if content:
            text_content += f"{content}\n"
        text_content += f"\n时间: {get_datetime()}"
        return self.send_text_message(text_content, touser=touser)
    
    def send_error(self, title: str, content: str, touser: Optional[str] = None) -> bool:
        """发送错误消息（使用text类型，支持普通微信）"""
        text_content = f"❌ {title}\n\n"
        if content:
            text_content += f"{content}\n"
        text_content += f"\n时间: {get_datetime()}"
        return self.send_text_message(text_content, touser=touser)
    
    def send_warning(self, title: str, content: str, touser: Optional[str] = None) -> bool:
        """发送警告消息（使用text类型，支持普通微信）"""
        text_content = f"⚠️ {title}\n\n"
        if content:
            text_content += f"{content}\n"
        text_content += f"\n时间: {get_datetime()}"
        return self.send_text_message(text_content, touser=touser)
    
    def send_info(self, title: str, content: str, touser: Optional[str] = None) -> bool:
        """发送信息消息（使用text类型，支持普通微信）"""
        text_content = f"ℹ️ {title}\n\n"
        if content:
            text_content += f"{content}\n"
        text_content += f"\n时间: {get_datetime()}"
        return self.send_text_message(text_content, touser=touser)
    
    def send_news_message(self, articles: list, touser: Optional[str] = None,
                          toparty: Optional[str] = None, totag: Optional[str] = None) -> bool:
        """
        发送卡片式消息（news类型）
        
        Args:
            articles: 文章列表，每个元素包含：
                - title: 标题（必填）
                - description: 描述（可选）
                - picurl: 图片URL（可选）
                - url: 点击跳转链接（可选）
            touser: 用户ID列表
            toparty: 部门ID列表
            totag: 标签ID列表
        
        Returns:
            是否发送成功
        """
        access_token = self.get_access_token()
        
        url = f"{self._send_msg_url}?access_token={access_token}"
        
        # 验证articles格式
        if not articles or not isinstance(articles, list):
            print("articles必须是包含至少一个元素的列表")
            return False
        
        # 格式化articles（确保每个article都有必需字段）
        formatted_articles = []
        for article in articles:
            if not isinstance(article, dict) or 'title' not in article:
                continue
            formatted_article = {
                "title": article.get('title', ''),
                "description": article.get('description', ''),
                "picurl": article.get('picurl', ''),
                "url": article.get('url', '')
            }
            formatted_articles.append(formatted_article)
        
        if not formatted_articles:
            print("没有有效的文章内容")
            return False
        
        data = {
            "touser": touser or "@all",
            "msgtype": "news",
            "agentid": self.agent_id,
            "news": {
                "articles": formatted_articles
            }
        }
        
        if toparty:
            data["toparty"] = toparty
        if totag:
            data["totag"] = totag
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=data)
                result = response.json()
                
                if result.get("errcode") == 0:
                    return True
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    print(f"发送卡片消息失败: {error_msg} (错误码: {result.get('errcode')})")
                    return False
        except Exception as e:
            print(f"发送卡片消息异常: {str(e)}")
            return False
    
    def create_menu(self, buttons: list) -> bool:
        """
        创建应用菜单
        
        Args:
            buttons: 菜单按钮列表，格式：
                [
                    {
                        "type": "click",
                        "name": "菜单名称",
                        "key": "菜单KEY"
                    },
                    {
                        "name": "父菜单",
                        "sub_button": [
                            {
                                "type": "click",
                                "name": "子菜单",
                                "key": "子菜单KEY"
                            }
                        ]
                    }
                ]
                一级菜单最多3条，子菜单最多5条
        
        Returns:
            是否创建成功
        """
        access_token = self.get_access_token()
        
        url = f"{self._create_menu_url}?access_token={access_token}&agentid={self.agent_id}"
        
        data = {
            "button": buttons[:3]  # 最多3个一级菜单
        }
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=data)
                result = response.json()
                
                if result.get("errcode") == 0:
                    return True
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    print(f"创建菜单失败: {error_msg} (错误码: {result.get('errcode')})")
                    return False
        except Exception as e:
            print(f"创建菜单异常: {str(e)}")
            return False
    
    def delete_menu(self) -> bool:
        """
        删除应用菜单
        
        Returns:
            是否删除成功
        """
        access_token = self.get_access_token()
        
        url = f"{self._delete_menu_url}?access_token={access_token}&agentid={self.agent_id}"
        
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url)
                result = response.json()
                
                if result.get("errcode") == 0:
                    return True
                else:
                    error_msg = result.get('errmsg', '未知错误')
                    print(f"删除菜单失败: {error_msg} (错误码: {result.get('errcode')})")
                    return False
        except Exception as e:
            print(f"删除菜单异常: {str(e)}")
            return False
