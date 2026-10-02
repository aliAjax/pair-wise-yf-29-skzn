# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱/移交、分析衍生关系、案件成员权限、法律保留、保留期限、不可变保管事件链和 JSON 报告导出。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8105>，默认数据库 `custody.db`。测试：

```bash
python3 -m unittest -v
```

演示身份：`custodian1`、`custodian2`、`analyst1`、`auditor1`、`outsider`。请求使用 `X-User-Id`。

## 主要接口

- `POST /api/cases`：创建案件，创建人自动成为保管员。
- `POST /api/cases/{id}/members`：授予 custodian、analyst 或 auditor 角色。
- `POST /api/cases/{id}/evidence`：以 Base64 入册证据，服务端计算 SHA-256 和大小。
- `GET /api/evidence/{id}`：查看元数据、完整保管事件链、完整性结果和衍生关系。
- `POST /api/evidence/{id}/open`：保管员开箱。
- `POST /api/evidence/{id}/transfer`：移交保管人并记录位置。
- `POST /api/evidence/{id}/derive`：分析员从已开箱证据创建衍生证据。
- `POST /api/evidence/{id}/hold`：审计员或案件创建人设置/解除法律保留。
- `POST /api/evidence/{id}/release`：存在法律保留时拒绝释放。
- `POST /api/evidence/{id}/dispatch`：保管员发出移交，证据进入在途状态（`in_transit=1`），复核与释放均拦截。
- `POST /api/evidence/{id}/accept`：保管员接收入库，在途移交完成，保管人变更为接收人。
- `POST /api/cases/{id}/release-reviews`：审计员或案件创建人创建释放复核单，按保留期限、法律保留、在途移交、派生分析件逐件判定可释放/不可释放并写明原因，同时固化每件证据的快照（哈希、保留状态、保管链长度与末位哈希、派生数）。
- `GET /api/cases/{id}/release-reviews`：列出案件的释放复核单。
- `GET /api/release-reviews/{id}`：查看复核单条目及当前证据状态。
- `POST /api/release-reviews/{id}/submit`：保管员按复核单提交释放。仅处理复核后未被改动的证据；被改动或保留状态变更的条目作废并写明原因；已释放/已跳过的条目不再重复写入。支持崩溃后重启再次调用继续，幂等不重复。
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告（含释放复核单与实际结果一致性）。
- 所有 `DELETE` 请求返回 405；证据和保管记录不提供删除接口。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
