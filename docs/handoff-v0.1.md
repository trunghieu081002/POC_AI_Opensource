# dpagent — Tài liệu tổng kết & bàn giao

**Dự án:** AI platform Opensource
**Chủ trì:** Hieu (hieudotrung2002@gmail.com)
**Ngày:** 09/09/2026
**Trạng thái:** v0.1 skeleton hoàn thành, sẵn sàng test luồng end-to-end

---

## 1. Vấn đề đang giải

Mỗi lần nhận dự án mới yêu cầu dựng pipeline xử lý dữ liệu bằng stack open-source (Airflow, dbt, MinIO, Trino, ClickHouse, v.v.), team phải tự SSH vào server, cài từng thứ bằng tay, cấu hình kết nối giữa các thành phần, viết systemd unit, debug lỗi permission/network. Một dự án tốn 3-4 tiếng chuẩn bị hạ tầng, và **kiến thức không tái sử dụng được** cho dự án sau vì mỗi server, mỗi distro, mỗi cấu hình lại phải mò lại từ đầu.

Nhu cầu:
- Tự động hoá luồng cài đặt full stack open-source bằng AI
- Đóng gói lại thành agent để triển khai cho dự án tiếp theo trong ~25 phút thay vì 3-4 tiếng
- Hỗ trợ đa distro (Oracle Linux, Ubuntu, RHEL, Debian)
- Có thể test flow bằng LLM free trước khi commit lên Claude production

## 2. Giải pháp: dpagent

Một Python service tên `dpagent` cài trực tiếp lên server đích. Nhận file YAML mô tả dự án, hỏi LLM để lập plan, rồi chạy shell có kiểm soát để dựng stack. Đóng gói một lần, triển khai không giới hạn dự án — chỉ file `project.yaml` thay đổi giữa các lần triển khai.

**Sơ đồ kiến trúc chi tiết đã publish thành trang web** (có 2 sơ đồ, giải thích 5 module, cấu trúc repo, lộ trình 7 ngày): xem file `dpagent-architecture.html` đính kèm hoặc mở artifact trong Claude workspace của chủ dự án.

## 3. Các quyết định đã chốt

Sau khi khảo sát các phương án, chọn được 4 quyết định nền tảng dưới đây. Mọi thảo luận từ đây trở đi đều dựa trên các lựa chọn này.

| Chủ đề | Lựa chọn | Vì sao |
|---|---|---|
| **Stack đích** | Batch ETL (Airflow + dbt + PostgreSQL/ClickHouse) **+** Lakehouse (MinIO + Trino + Iceberg + Spark) | Bao phủ 2 nhóm use case phổ biến nhất: DWH batch báo cáo, và data lake hiện đại tách compute/storage |
| **Mô hình chạy** | Agent self-hosted trên chính server đích | Không phụ thuộc SSH inbound từ dev, không cần control-plane tập trung, đơn giản để triển khai on-prem |
| **Cách sinh lệnh** | AI thuần — LLM sinh shell từng bước, KHÔNG dùng Ansible/Terraform | Linh hoạt tối đa cho giai đoạn khám phá. Chấp nhận trade-off về idempotency & audit; bù bằng 4 lớp guardrail |
| **Target OS** | Đa distro (RHEL family + Debian family) | Client có server đa dạng; agent phải detect và chọn `apt` hoặc `dnf` tự động |

**Cảnh báo về "AI thuần":** hướng này linh hoạt nhất nhưng có 3 rủi ro production đã ghi nhận:
- Không idempotent — chạy lại lần 2 dễ hỏng state
- Khó audit — khi có sự cố, khó truy vết chính xác agent đã làm gì
- Khó rollback — một câu `rm -rf` sai chỗ là hết

Về sau nếu thấy nặng, có thể chuyển dần sang mô hình lai: AI điều phối kế hoạch + Ansible/shell script cho các bước lặp lại đã ổn định.

## 4. Kiến trúc — 5 module bên trong agent

Mỗi module một trách nhiệm rõ ràng. Analogy nhà bếp giúp nhớ vai trò khi đọc code.

### 4.1 agent-core — orchestrator
Đọc spec, phân công các module khác, quản lý plan/state, giao tiếp với user qua CLI. Không tự biết cài gì — hỏi LLM rồi giao việc.
> Bếp trưởng đọc thực đơn, phân công, ra quyết định — không tự nấu.

### 4.2 bash-executor — shell wrapper có guardrails
Mọi lệnh shell đi qua module này. Có dry-run, timeout, log full stdin/stdout/stderr, whitelist prefix, blacklist tuyệt đối. Chống `rm -rf /` ngay từ tay.
> Bàn tay có găng chống lửa — công thức bảo "đổ xăng vào chảo" thì tay không chịu làm.

### 4.3 os-detector — distro sniff
Đọc `/etc/os-release`, chọn package manager (`dnf`/`apt`), service manager, firewall (`firewalld`/`ufw`). Kết quả này LLM dùng để chọn nhánh trong skill.
> Thư ký nhìn quanh bếp: nhà này bếp gas hay bếp từ?

### 4.4 state-store — SQLite journal
Lưu plan, checkpoint sau mỗi bước thành công, log lệnh, snapshot cho rollback. Nhờ có sổ này, mất điện giữa chừng chạy lại sẽ tiếp từ chỗ dừng, không cài lại từ đầu.
> Sổ ghi chép: "đã ướp thịt lúc 19:03, đang luộc mì, chưa làm sốt."

### 4.5 skill-library — component recipes
Thư mục các file `skills/*.md`. Mỗi file mô tả cách cài 1 component theo 6 chương chuẩn. Đây là phần đầu tư nhất — từ dự án thứ 2 chỉ mở rộng thư viện, không sửa agent.
> Tủ sách công thức. Thêm món mới cho tiệc sau chỉ cần viết thêm 1 cuốn bỏ vào tủ.

## 5. Luồng làm việc end-to-end

### 5.1 Bootstrap agent lên server mới (chỉ 1 lần, 2 phút)

```bash
ssh root@<target-server>
curl -fsSL https://<repo>/bootstrap.sh | sudo bash
```

Script tự cài Python, tạo user `dpagent`, clone repo agent + skill-library, cài dependencies, tạo systemd unit. Xong bạn có lệnh `dpagent` dùng được.

### 5.2 Viết file spec cho dự án (5 phút)

`project.yaml` là **file duy nhất khác nhau giữa các dự án**:

```yaml
project: acme-etl
components:
  - postgres:
      version: 15
      databases: [airflow_meta, warehouse]
  - airflow:
      version: 2.8
      executor: LocalExecutor
      backend: postgres        # trỏ ngược lên postgres bên trên
  - dbt:
      adapter: postgres
      target_db: warehouse
users:
  - name: acme_admin
    ssh_key: ssh-rsa AAAA...
```

### 5.3 Lập plan (30 giây — AI vào cuộc lần đầu)

```bash
dpagent plan project.yaml
```

Nội bộ agent làm 4 việc:
1. Đọc yaml → biết cần 3 skill: `postgres`, `airflow`, `dbt`
2. `os-detector` detect OS → biết Ubuntu 22.04 → chọn `apt`
3. Đọc 3 file `skills/*.md`
4. Gọi LLM với prompt: spec + skills + OS info → nhận về JSON plan chi tiết

Plan được lưu vào SQLite, in ra cho user duyệt dạng cây.

### 5.4 Duyệt & thực thi (15-20 phút, chủ yếu chờ apt tải gói)

```bash
dpagent apply --dry-run    # in mọi lệnh sẽ chạy, không chạy thật
sudo -E dpagent apply       # chạy thật
```

Với **mỗi step**, agent làm 4 việc:
1. Check whitelist prefix — ngoài whitelist thì hỏi user confirm
2. `bash-executor` chạy lệnh, log JSONL vào `/var/log/dpagent/`
3. Exit code 0 → ghi checkpoint SQLite → chạy step tiếp theo
4. Fail → DỪNG, gửi error về LLM hỏi cách sửa → hiển thị lệnh sửa → user duyệt → retry

### 5.5 Verify (30 giây)

```bash
dpagent verify
```

Chạy health check của từng skill (mỗi `skills/*.md` có section "verify") và in bảng kết quả.

### 5.6 Dự án tiếp theo — chỉ đổi 1 file

Sếp giao dự án Lakehouse, server RHEL 9, cần MinIO + Trino + Iceberg. Viết `project.yaml` mới (~30 dòng), chạy `plan → apply → verify`. **Agent code + skill library không đổi.** Nếu Iceberg chưa có skill, viết thêm `skills/iceberg.md` một lần → toàn bộ dự án sau đều dùng được.

Đó chính là "đóng gói agent để đi triển khai" nói ở đầu.

## 6. Skill library — cấu trúc chuẩn

Mỗi component là một file `.md` trong `skills/`, đúng 6 chương:

```markdown
# <component-name>

## trigger
Khi nào dùng skill này (ví dụ: khi project.yaml có `<component>` trong components).

## prerequisites
Package/port/user cần có trước khi cài.

## install (debian)
Các lệnh shell cho Ubuntu/Debian, 1 lệnh/dòng, idempotent.

## install (rhel)
Các lệnh shell cho RHEL/Oracle Linux/Rocky/AlmaLinux/Fedora.

## config
Template config file. Dùng cú pháp Jinja2 `{{ var }}` cho giá trị lấy từ spec.

## verify
Lệnh trả về exit 0 khi service khỏe.

## rollback
Lệnh gỡ hoàn toàn khi cần dọn.
```

**Nguyên tắc bất di bất dịch:** chất lượng skill quyết định chất lượng plan LLM sinh ra. Skill viết ẩu → plan tệ. Đầu tư kỹ vào skill đầu tiên (`postgres.md`) làm template chuẩn, rồi nhân bản cho các component khác.

Tối thiểu cần 10 skill cho 2 stack đã chốt:
1. `base-hardening.md` — user, ssh, ntp, firewall, SELinux
2. `postgres.md` ✅ *(đã viết đầy đủ trong v0.1)*
3. `clickhouse.md`
4. `airflow.md`
5. `dbt.md`
6. `minio.md`
7. `hive-metastore.md`
8. `iceberg-catalog.md`
9. `trino.md`
10. `spark.md`

## 7. Guardrails — 4 lớp bảo vệ bắt buộc

Vì đã chọn "AI thuần" (LLM sinh shell trực tiếp), phải có 4 lớp guardrail dưới đây, **không được bỏ**:

### Whitelist prefix (auto-allowed)
Các lệnh chạy không cần hỏi: `apt`, `apt-get`, `dpkg`, `dnf`, `yum`, `rpm`, `systemctl`, `mkdir`, `cp`, `mv`, `chown`, `chmod`, `useradd`, `python3`, `pip`, `curl`, `wget`, `docker`, `firewall-cmd`, `sudo -u postgres`, ...

### Blacklist patterns (refuse always)
Regex bắt các lệnh nguy hiểm, refuse dù user có OK cũng không chạy:
- `rm -rf /` và `rm -rf /*`
- `dd of=/dev/sd*` (ghi vào raw disk)
- `mkfs.*` (format filesystem)
- `> /etc/passwd`, `> /etc/shadow`, `> /etc/sudoers`
- `chmod -R 777 /`
- Fork bomb `:(){ :|: & };:`
- `curl ... | bash` (pipe to shell blind)
- `reboot`, `shutdown`, `halt`, `poweroff`
- `userdel -r root`

### Snapshot filesystem trước khi thay đổi lớn
LVM snapshot hoặc btrfs subvol (nếu có) trước các bước ảnh hưởng nhiều file.

### Log kèm justification
Mọi lệnh `sudo` phải log kèm "justification" bằng English do AI tự sinh — để sau này grep log truy vết được vì sao AI quyết định làm vậy.

**Test:** đã viết 13 test case cho guardrails trong `tests/test_guardrails.py`, tất cả pass. Chạy `pytest tests/` bất cứ khi nào sửa `guardrails.py`.

## 8. LLM provider — chiến lược multi-provider

Dùng thư viện [LiteLLM](https://github.com/BerriAI/litellm) để trừu tượng hoá — 1 dòng đổi provider.

### Provider hỗ trợ (chỉ đổi biến `DPAGENT_MODEL`)

| Provider | Free? | Model string | Ghi chú |
|---|---|---|---|
| **Google Gemini** | ✅ 1500 req/day | `gemini/gemini-2.0-flash` | **Khuyến nghị để test flow** — free tier rộng nhất |
| Groq | ✅ generous | `groq/llama-3.3-70b-versatile` | Nhanh, Llama 70B |
| OpenRouter | Một số free | `openrouter/deepseek/deepseek-chat` | Route đến nhiều provider |
| Ollama (local) | ✅ 100% | `ollama/qwen2.5-coder:32b` | Cần GPU hoặc 32GB+ RAM |
| OpenAI | Paid rẻ | `openai/gpt-4o-mini`, `openai/gpt-5-mini` | Thử tên model có trong tài khoản |
| Anthropic Claude | Paid | `anthropic/claude-sonnet-4-5` | Dùng khi lên production |

### Chi phí ước tính mỗi lần `plan`

Giả định 1 skill ~200 dòng, prompt tổng ~5-10k tokens:

| Provider | Chi phí/plan |
|---|---|
| Gemini Flash free tier | $0 |
| Groq free tier | $0 |
| Ollama local | $0 (chỉ điện) |
| GPT-4o-mini | ~$0.005 |
| Claude Sonnet 4.5 | ~$0.20 |

Full stack (5+ skills) = 5-10× số trên.

## 9. Cấu trúc code v0.1 (đã hoàn thành, đóng gói trong `dpagent-v0.1.tar.gz`)

```
dpagent/
├── README.md
├── pyproject.toml              # deps: click, litellm, pyyaml, jinja2, rich
├── bootstrap.sh                # 1 lệnh cài agent lên server mới
├── .env.example                # config LLM provider
├── .gitignore
│
├── dpagent/                    # Python package
│   ├── __init__.py
│   ├── cli.py                  # click commands: info, plan, apply, verify
│   ├── planner.py              # đọc yaml + skills + OS → gọi LLM → parse plan
│   ├── bash_executor.py        # subprocess + guardrails + log
│   ├── guardrails.py           # whitelist/blacklist rules
│   ├── os_detector.py          # đọc /etc/os-release
│   ├── state_store.py          # SQLite: plans, steps, checkpoints
│   ├── llm.py                  # LiteLLM wrapper — provider agnostic
│   └── prompts/
│       └── plan_system.md      # system prompt cho lập plan
│
├── skills/                     # thư viện cài components
│   ├── _template.md            # template 6 chương
│   └── postgres.md             # ✅ đầy đủ Ubuntu + RHEL
│
├── examples/
│   └── postgres-only.yaml      # spec mẫu test PostgreSQL
│
└── tests/
    └── test_guardrails.py      # 13 tests, đã pass
```

## 10. Cách chạy — test luồng free trong ~5 phút

**Test planning từ laptop dev, chưa cần server:**

```bash
# 1. Bung file
tar xzf dpagent-v0.1.tar.gz && cd dpagent

# 2. Lấy Gemini API key free tại https://aistudio.google.com/app/apikey

# 3. Cài + cấu hình
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
# Sửa .env: điền GEMINI_API_KEY
export $(grep -v '^#' .env | xargs)

# 4. Sanity check (không gọi LLM)
dpagent info

# 5. Test luồng plan — an toàn tuyệt đối, chỉ gọi LLM, không đụng hệ thống
dpagent plan examples/postgres-only.yaml --fake-os ubuntu
```

**Test apply trên VM (Ubuntu 22 hoặc Oracle Linux 8/9):**

```bash
sudo -E dpagent apply --dry-run    # in ra mọi lệnh, chưa chạy
sudo -E dpagent apply               # chạy thật, có checkpoint SQLite
```

**Đổi provider chỉ 1 dòng:**

```bash
export DPAGENT_MODEL=groq/llama-3.3-70b-versatile    # Groq free
export DPAGENT_MODEL=openai/gpt-5-mini                # GPT
export DPAGENT_MODEL=ollama/qwen2.5-coder:32b         # Local
export DPAGENT_MODEL=anthropic/claude-sonnet-4-5      # Production
```

**Thêm skill mới** (VD ClickHouse):

```bash
cp skills/_template.md skills/clickhouse.md
# Điền 6 chương, xong. Không sửa 1 dòng code Python nào.
```

## 11. 4 checkpoint xác nhận từng bước hoạt động

| Lệnh | Xác nhận | Trạng thái |
|---|---|---|
| `dpagent info` | CLI + OS detect + model config OK | ⏳ Chờ test |
| `dpagent plan examples/postgres-only.yaml --fake-os ubuntu` | LLM integration + skill parsing + plan JSON OK | ⏳ Chờ test |
| `sudo -E dpagent apply --dry-run` | Guardrails classify được từng lệnh | ⏳ Chờ test |
| `sudo -E dpagent apply` cài PostgreSQL thật | End-to-end works | ⏳ Chờ test |

Đến checkpoint cuối là có MVP demo được cho stakeholder.

## 12. Lộ trình 7 ngày → MVP demo

| Ngày | Việc | Trạng thái |
|---|---|---|
| Chuẩn bị (30 phút) | VM test + API key + repo trống | Việc của người triển khai |
| 1 (nửa ngày) | Skeleton + CLI rỗng | ✅ **v0.1 đã có** |
| 2-3 (1.5 ngày) | bash-executor + guardrails + os-detector + skills/postgres.md | ✅ **v0.1 đã có** |
| 4-5 (2 ngày) | LLM integration + state-store | ✅ **v0.1 đã có** |
| 6-7 (2 ngày) | Wire up apply + verify + demo E2E | ⏳ Chờ test trên VM thật |

**Từ tuần 2 trở đi:** mở rộng skill library, không sửa agent nữa.
- Tuần 2: `airflow.md`, `dbt.md`, `clickhouse.md` → dựng full ETL stack
- Tuần 3: `minio.md`, `trino.md`, `spark.md`, `iceberg.md`, `hive-metastore.md` → dựng lakehouse
- Tuần 4: web UI review plan, đóng gói `pyinstaller`, docs vận hành, test 3 distro

## 13. Rủi ro cần chú ý từ ngày 1

### Cost
Mỗi `plan` đính kèm skill (full content) → prompt ~30-50k tokens với stack lớn. Trên Claude Sonnet giá ~$0.15-0.25/plan. Bình thường không sao nhưng nên track. **Trên free tier (Gemini/Groq) thì $0**, dùng test thoải mái.

### Secret handling
`project.yaml` sẽ chứa SSH keys, DB passwords, S3 credentials. Phải support đọc từ env var (`${DB_PASS}`) hoặc vault **ngay từ ngày đầu** — không để plaintext trong yaml commit vào git.

### Skill quality
Skill viết ẩu → AI plan tệ. Cần test parser: mỗi skill phải parse được, có đủ 6 section, có install cho cả `apt` và `dnf`. Fail test là fail CI.

## 14. Đính kèm

Người đọc tài liệu này nên có 2 file kèm theo:

1. **`dpagent-v0.1.tar.gz`** (14KB) — skeleton code đầy đủ, 15 files, chạy được. Chi tiết trong section 9.
2. **`dpagent-architecture.html`** — trang kiến trúc trực quan với 2 sơ đồ (tổng thể + vòng đời step), giải thích 5 module, lộ trình 7 ngày. Xem trực tiếp trong trình duyệt.

Hai file này + tài liệu Markdown này là bộ handoff hoàn chỉnh.

## 15. FAQ

**Hỏi:** Vì sao không dùng Ansible ngay từ đầu cho ổn định?
**Đáp:** Đã cân nhắc. Chọn "AI thuần" cho v0.1 vì linh hoạt và học nhanh. Nếu về sau vận hành thấy nặng, chuyển sang mô hình lai (AI điều phối + Ansible cho các bước ổn định) là đường đi hợp lý.

**Hỏi:** Vì sao là Python chứ không phải Go?
**Đáp:** Ecosystem AI (LiteLLM, tokenizers, prompt libraries) đang tập trung ở Python. Deploy là 1 systemd unit + venv, không cần Go binary. Nếu cần binary sau này, `pyinstaller` gói được.

**Hỏi:** Có thể chạy agent trong container thay vì trên host trực tiếp không?
**Đáp:** Được, nhưng agent cần đụng đến `/etc`, `/opt`, `systemctl`, `apt`/`dnf` của host — nên phải mount rất nhiều thứ. Đơn giản nhất là chạy trên host qua systemd. Trong Kubernetes, mô hình sẽ khác (Operator pattern) — chưa cover trong v0.1.

**Hỏi:** LLM có sinh ra lệnh sai không?
**Đáp:** Có. Đó là lý do có 4 lớp guardrail (whitelist/blacklist/dry-run/user confirm) và nhánh "on-fail ask LLM to fix" trong vòng đời step. Chấp nhận LLM sai, thiết kế để phát hiện sớm và không gây thiệt hại.

**Hỏi:** Nếu dự án cần component không có trong skill library thì sao?
**Đáp:** Viết thêm 1 file `skills/<name>.md` theo template 6 chương. Không sửa code Python. Đây chính là điểm "mở rộng dễ" của kiến trúc.

**Hỏi:** Agent này có thay thế được DevOps engineer không?
**Đáp:** Không. Nó tự động hoá bước cài đặt lặp lại. DevOps vẫn cần cho: capacity planning, security review, cấu hình phức tạp không có trong skill, xử lý sự cố production, tuning performance. Agent giải phóng 3-4 tiếng cài đặt/dự án để DevOps làm việc khác giá trị hơn.

---

*Tài liệu này tổng kết mọi quyết định + code đã chốt tính đến 09/09/2026. Cập nhật khi có thay đổi lớn về kiến trúc hoặc scope.*
