# App Store 与华为应用市场发布清单

## 0. 当前工程基线

- [x] Web 生产构建通过
- [x] Android 调试 APK 构建通过
- [x] Android Release AAB 构建通过（当前未使用发布方密钥签名）
- [x] Android Lint 无应用错误
- [x] Capacitor iOS/Android 工程已生成
- [x] iOS Privacy Manifest、应用图标、启动图已生成并通过 plist 语法校验
- [x] 隐私同意、使用条款、支持入口和应用内删号已实现
- [ ] 完整 Xcode 下的 iOS Archive、真机与 TestFlight 验证
- [ ] 公开 HTTPS 后端及 Web 法律页面部署

## 1. 冻结身份

- [ ] 确认“冰焰智研 / FrostFire AI”名称、图标和商标风险
- [ ] 确认最终 Bundle ID / Application ID：`com.pythonerjavaer.frostfireai`
- [ ] 确认版本 `1.0.0`、iOS build number 和 Android versionCode
- [ ] 提供运营主体法定名称、地址、支持邮箱和隐私联系邮箱
- [ ] 确认目标国家/地区、年龄分级、类别与定价

Bundle ID 创建商店记录后不应随意更换。若要使用不同标识，应先修改 Capacitor、Xcode 和 Gradle 配置，再创建商店记录。

## 2. 生产后端

- [ ] 选择部署平台和数据所在地区
- [ ] 配置长期持久化磁盘或迁移到托管 PostgreSQL
- [ ] 配置 `OPENAI_API_KEY` 与高强度 `JWT_SECRET`
- [ ] 设置精确的 `CORS_ORIGINS`
- [ ] 全站 HTTPS，确认 `/api/health` 可公开访问
- [ ] 配置加密备份、恢复演练、日志保留、告警与事故联系人
- [ ] 以非管理员账号执行注册、登录、上传、聊天、删除和删号端到端测试
- [ ] 用生产 API URL 重新构建并同步移动端：

```bash
cd frontend
VITE_API_BASE_URL=https://api.example.com/api npm run mobile:sync
```

## 3. 隐私、合规和内容

- [ ] 将运营主体与联系信息写入隐私政策和商店后台
- [ ] 公开隐私政策、使用条款、支持中心 HTTPS URL
- [ ] 核对 App Store Privacy Nutrition Labels / 华为隐私标签与实际数据流一致
- [ ] 明示内容会发送到 OpenAI API，并保留注册时的主动同意
- [ ] 为每个第三方 SDK/处理方记录用途、数据类型、保留和跨境安排
- [ ] 确认账号删除后数据库、备份和安全日志的实际删除/保留规则
- [ ] 为合同与金融内容准备专业边界、投诉和升级流程
- [ ] 请目标市场律师复核政策、条款、金融/法律定位和运营资质

## 4. Apple App Store

- [ ] 使用有效 Apple Developer 账号；涉及法律/金融服务时确认应使用的组织主体与资质
- [ ] 在 App Store Connect 创建 App 记录、SKU、Bundle ID 和版本
- [ ] 使用完整 Xcode 设置 Team、Signing & Capabilities
- [ ] 真机测试网络、键盘、安全区、深色界面、上传和删号
- [ ] Archive 并上传 build；先经 TestFlight 内部测试
- [ ] 上传 iPhone/iPad 所需尺寸截图和 1024×1024 图标
- [ ] 填写隐私政策 URL、支持 URL、年龄分级、出口合规和 App Privacy
- [ ] 在 Review Notes 提供专用审核账号、功能路径和 OpenAI 数据披露说明
- [ ] 选择 build，完成定价和可用地区，最后显式提交审核

本仓库已声明不使用受限加密算法（`ITSAppUsesNonExemptEncryption=false`），提交人仍需基于实际功能回答出口合规问卷。

## 5. 华为应用市场

- [ ] 使用已实名认证的华为开发者账号并完成所需商户/企业资料
- [ ] 在 AppGallery Connect 创建应用并确认包名
- [ ] 创建并安全保管 Android 上传/发布签名密钥
- [ ] 配置 Gradle release signing；不要把密钥或密码提交到 Git
- [ ] 生成并验证签名 AAB/APK，完成目标华为设备兼容测试
- [ ] 填写应用介绍、分类、隐私政策 URL、权限说明、分级和服务地区
- [ ] 上传手机/平板截图、图标与必要的软件资质
- [ ] 提供审核账号和测试说明，完成最后显式提交审核

## 6. 发布前验收

- [ ] `python -m pytest -q`
- [ ] `npm run build && npm audit`
- [ ] `./gradlew bundleRelease lint`
- [ ] Xcode Archive / Analyze 无阻断问题
- [ ] 对仓库当前文件和完整 Git 历史执行 secrets scan
- [ ] 用生产包完成一次真实 OpenAI 聊天、文档上传、来源引用和删号
- [ ] 弱网、离线、Token 过期、API 限流、超大文件和错误密码均有可理解提示
- [ ] 审核账号没有个人数据，审核资料无版权或保密问题
- [ ] 发布后监控、支持和回滚负责人已就位

## 7. 不可自动代替提交人的动作

以下动作必须由账号持有人在平台页面或签名环境中确认：开发者协议、法律主体/资质声明、付费或税务信息、证书和发布密钥授权、隐私问卷的法律确认，以及最后的“提交审核”。这些信息齐全后，Codex 可以继续协助操作页面、生成签名构建和逐项核验，但不会猜测或伪造资料。
