# Cookie配置说明

## 配置Cookie的方法

### 方法1：通过企业微信应用设置（推荐）

1. 启动程序后，在企业微信应用中发送以下消息：
   ```
   cookie: <你的Cookie内容>
   ```
   
2. 程序会自动验证Cookie并保存

3. 发送 `verify` 命令验证Cookie是否有效

### 方法2：直接编辑文件

1. 编辑 `config/cookies.txt` 文件

2. 将Cookie粘贴到文件中（不需要创建新文件，如果文件不存在，程序会自动创建）

3. Cookie格式示例：
   ```
   QC005=xxx; QC006=xxx; QC010=xxx; QK_Token=xxx; ...
   ```

4. 保存文件后，重启程序

## 如何获取Cookie

### 步骤1：登录夸克网盘

1. 打开浏览器（推荐Chrome或Edge）
2. 访问 https://pan.quark.cn
3. 登录你的夸克网盘账号

### 步骤2：打开开发者工具

- **Windows/Linux**: 按 `F12` 或 `Ctrl+Shift+I`
- **Mac**: 按 `Cmd+Option+I`

### 步骤3：查看Cookie

1. 在开发者工具中，切换到 **Network（网络）** 标签
2. 刷新页面（按 `F5` 或 `Cmd/Ctrl+R`）
3. 在请求列表中，点击任意一个请求（通常是第一个请求）
4. 在右侧面板中，找到 **Request Headers（请求头）** 部分
5. 找到 **Cookie** 字段
6. 复制完整的Cookie值（从 `QC005=` 开始到结尾的所有内容）

### 步骤4：配置Cookie

将复制的Cookie通过以下方式之一配置：

- **方式A（推荐）**：在企业微信应用中发送 `cookie: <复制的Cookie>`
- **方式B**：编辑 `config/cookies.txt` 文件，将Cookie粘贴进去

## Cookie格式说明

Cookie应该是一串类似这样的文本：
```
QC005=xxx; QC006=xxx; QC010=xxx; QK_Token=xxx; ...
```

**重要提示**：
- 不要包含 `Cookie: ` 前缀
- 不要包含换行符
- 确保Cookie完整（从第一个键值对到最后）
- Cookie值中可能包含特殊字符，直接复制即可

## 验证Cookie

配置Cookie后，可以通过以下方式验证：

1. **在企业微信应用中发送** `verify` 命令
2. **查看程序日志**，确认Cookie是否加载成功
3. **尝试转存一个文件**，如果成功说明Cookie有效

## 常见问题

### Q: Cookie多久会过期？
A: Cookie的有效期取决于夸克网盘的策略，通常几天到几周不等。如果转存失败，可能是Cookie过期，需要重新获取并更新。

### Q: 如何更新Cookie？
A: 重新获取Cookie后，通过企业微信发送 `cookie: <新Cookie>` 即可更新。

### Q: Cookie配置后程序无法启动？
A: 检查Cookie格式是否正确，确保没有多余的空格或换行。如果 `config/cookies.txt` 文件有问题，可以删除该文件后重新配置。

### Q: 可以在多个设备使用同一个Cookie吗？
A: 可以，但需要注意Cookie可能有过期时间。如果其中一个设备更新了Cookie，其他设备需要同步更新。

