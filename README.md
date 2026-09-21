# nuaa-phyexp-lab

> 南京航空航天大学物理实验中心**数据研究** + 物理实验**预约辅助工具**
> Data research on the NUAA Physics Experiment Center and a booking assistant for its physics-lab reservation system.

- 目标系统（官网）：<http://phylab.nuaa.edu.cn/>
- 目标系统（预约选课·**天目湖校区入口**）：<https://phyexp.nuaa.edu.cn/tianmuhu/wechat/login>（页面标题「实验助手」）
- 后端 API 基址：`https://phyexp.nuaa.edu.cn/tianmuhu/api`（**PostgREST 风格查询 + Bearer JWT**）
- 姊妹项目：[NUAA-Snatcher](https://github.com/dboycht/NUAA-Snatcher) —— 那个是**教务系统**（金智 eams）选课，与本项目的**物理实验预约系统是两套完全不同的系统**；本项目移植其登录态获取、预发射提交、限速退避等工程经验。

---

## 项目目标

| 方向 | 内容 | 产出 |
| --- | --- | --- |
| ① 数据研究 | 实验项目清单、开放时段、容量与余量、放课时间与退课规律 | `docs/排课与放课规律.md` |
| ② 接口研究 | 登录方式、接口清单、参数与响应字段 | `docs/接口逆向.md` |
| ③ 预约辅助 | 登录态复用、余量监控、按放闸时间自动提交（低频 + 限速退避） | `src/phyexp_lab/`（CLI → GUI） |

> 当前处于**阶段 0：接口侦察**。已经完成的部分：
> - **前端接口侦察（已完成）**：API 基址、鉴权链路（`POST rest/rpc/login` + `localStorage.token` 的 Bearer JWT）、
>   PostgREST 数据层、29 张数据表、选课关键字段（`max_student_number` / `current_student_number` / `schedule_status`）、
>   选课与退课写接口 —— 全部记入 [`docs/接口逆向.md`](docs/接口逆向.md)，并逐条标注了**证据级别**（实测 / 源证 / 待验）。
> - **待做**：用一次真实登录抓包，确认选课请求体与响应判据；在那之前 `api.py` 相关方法**显式抛 `NotImplementedError`**，不猜参数、不伪装成功。

---

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 使用

```bash
python run.py version              # 查看版本
python run.py status               # 查看会话状态 / token 有效期 / 数据目录
python run.py login                # 打开浏览器，由你手动完成登录并保存会话
python run.py recon                # 侦察：登录 + 把请求/响应（HAR + JSONL）录到本地，供接口逆向
python run.py scrub <file.har>     # 脱敏 HAR：抹掉 Cookie/Authorization/密码 MD5 与敏感响应体后再分析
python run.py logout               # 删除本地会话文件
```

`recon` 会弹出真实 Chromium 窗口：**你在窗口里自行完成登录（含验证码），程序不接触你的密码**；登录后请在窗口里点进「预约选课」各页面，工具会把所有请求记录下来。

## 数据存放（一律不进仓库）

所有会话、Cookie、HAR、日志都写在 **`%LOCALAPPDATA%\PhyExpLab\`**（可用环境变量 `PHYEXP_HOME` 覆盖）：

```
%LOCALAPPDATA%\PhyExpLab\
├─ session\state.json      # Playwright storage_state（含会话 Cookie，敏感，勿外传）
├─ recon\*.har             # 完整 HAR（含响应体，逆向主依据）
├─ recon\*.jsonl           # 逐条请求摘要（已脱敏：Cookie/Authorization 只留长度）
└─ logs\
```

## 目录结构

```
nuaa-phyexp-lab/
├─ run.py                        # 开发期启动入口（自带 sys.path 兜底）
├─ src/phyexp_lab/
│  ├─ __init__.py                # 唯一版本来源 __version__
│  ├─ config.py                  # 路径 / URL / UA / 反检测脚本
│  ├─ session.py                 # Playwright 弹窗登录 + 会话持久化
│  ├─ recon.py                   # 请求采集（HAR + 脱敏 JSONL）
│  ├─ api.py                     # 接口封装（待逆向完成后实现）
│  ├─ models.py                  # 数据模型（实验 / 时段 / 预约结果）
│  ├─ monitor.py                 # 余量监控（待实现）
│  ├─ grabber.py                 # 预发射提交引擎（待实现）
│  └─ cli.py                     # 命令行入口
└─ docs/
   ├─ 接口逆向.md                 # 已实测事实 + 待填接口清单
   └─ 排课与放课规律.md            # 数据研究问题与方法
```

## 隐私与合规

- 账号密码、会话 Cookie、HAR **绝不进入仓库**（见 `.gitignore` 与上面的数据目录约定）；采集日志对敏感头做了脱敏。
- **HAR 是敏感文件**：它包含 `Authorization: Bearer <jwt>`、Cookie、以及登录请求体里的**密码 MD5**（可离线爆破）。
  因此本仓库提供 `python run.py scrub`：默认输出 `*.scrubbed.har`（`--in-place` 会先备份 `.bak`），
  抹掉敏感头值、敏感接口的请求体/响应体，并**复检**是否还有残留（有残留则退出码 1）。
- 请求一律**低频 + 限速退避**，不做高频轰炸；工具仅个人学习使用，请遵守学校相关管理规定。
- 本项目不做验证码破解：验证码由使用者本人在浏览器里输入。

## 致谢

- 登录反检测与提交引擎思路移植自同作者的 [NUAA-Snatcher](https://github.com/dboycht/NUAA-Snatcher)。

## License

MIT，见 [LICENSE](LICENSE)。
