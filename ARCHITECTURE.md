# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Hệ thống là plain Python async (không LLM, không framework agent). Mỗi agent là một coroutine in-process; `cli.py` chạy tuần tự từng case, trong một case các specialist chạy song song bằng `asyncio.gather`.

```text
inputs/<case_id>.json
   │  (cli: case_received)
   ▼
Coordinator ──task_assigned──► order-agent ─┐   get_order, get_order_items,
   │                                        │   get_sellers, get_product_context
   ├──task_assigned──► payment-agent ───────┤   get_order_payments, get_payment_timeline
   ├──task_assigned──► refund-agent ────────┤   get_refund_timeline
   ├──task_assigned──► shipment-agent ──────┤   get_shipment_summary
   └──task_assigned──► policy-agent ────────┤   get_policy(policy_version)
                                            │   (MCP Evidence Gateway, tool_result_consumed)
        ◄──────────handoff (EVIDENCE_*)─────┘
   │
   ▼  Evidence store (tool_name → ref, domain, data) — riêng cho case
policy-agent: rules.decide(case, data) ── policy_decided (decision_code = primary_issue)
   │
   ▼  handoff coordinator → verifier (VERIFY_OUTPUT)
Verifier: sửa/hạ cấp theo invariant ── verification_completed (PASS | FIXED | DOWNGRADED)
   │  handoff verifier → coordinator (OUTPUT_<code>)
   ▼
outputs/<case_id>.json + traces/trace.jsonl   (cli: case_finalized)
```

Code: `src/student_agent/workflow.py` (điều phối, evidence, trace, verifier), `src/student_agent/rules.py` (quyết định nghiệp vụ thuần).

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case input | Giao việc cho 5 specialist, gom evidence store, gọi decision layer, chuyển cho verifier | `task_assigned` ×5, `handoff` → verifier |
| Order/item (`order-agent`) | `case_id`, `claimed_order_id` | Lấy order, items, sellers, product context | `tool_result_consumed` mỗi tool, `handoff` → coordinator |
| Payment (`payment-agent`) | `case_id`, `claimed_order_id` | Lấy payments và payment timeline | như trên |
| Refund (`refund-agent`) | `case_id`, `claimed_order_id` | Lấy refund timeline; lỗi tool = "không có dữ liệu refund" | như trên (thường `EVIDENCE_MISSING`) |
| Shipment (`shipment-agent`) | `case_id`, `claimed_order_id` | Lấy shipment summary | như trên |
| Policy (`policy-agent`) | `case_id`, `policy_version`; sau đó toàn bộ evidence data | Lấy policy; chạy `rules.decide` để chọn primary_issue, status, refund, action | `handoff` → coordinator, `policy_decided` |
| Verifier | Output nháp + evidence store của case | Kiểm tra/sửa invariant, validate schema | `verification_completed`, `handoff` → coordinator |

Quyền gọi tool (enforce bằng `AGENT_TOOLS` trong `workflow.py`; mỗi specialist chỉ lặp qua tool của mình):

| Actor | Tool được phép |
| --- | --- |
| order-agent | `get_order`, `get_order_items`, `get_sellers`, `get_product_context` |
| payment-agent | `get_order_payments`, `get_payment_timeline` |
| refund-agent | `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| coordinator, verifier | không gọi MCP |

`get_customer_history` không được dùng (order không có `customer_unique_id`).

## 3. A2A protocol

- **Envelope**: message in-process, không serialize. Giao việc = `task_assigned` (actor=coordinator, target=agent, `attributes.tools` = danh sách tool). Kết quả = ghi vào evidence store của case + `handoff` (actor=agent, target=coordinator, `decision_code` ∈ `EVIDENCE_READY | EVIDENCE_PARTIAL | EVIDENCE_MISSING`, attributes: `tools_ok`, `tools_total`, trạng thái từng tool `OK | NOT_FOUND | INVALID | TIMEOUT | ERROR`).
- **Correlation**: mọi event và mọi MCP call mang `case_id` của case hiện tại; evidence store tạo mới trong `solve_case`, không có state toàn cục.
- **Điều kiện handoff**: specialist handoff sau khi thử hết tool của mình (thành công hoặc thất bại). Coordinator chỉ chuyển sang decision khi `gather` hoàn tất; verifier chỉ chạy sau `policy_decided`.
- **Timeout**: mỗi MCP call `asyncio.wait_for` 60 s, tối đa 2 lần thử; httpx client có timeout 300 s (gateway).
- **Không vòng lặp**: luồng là DAG cố định coordinator → specialists → policy → verifier → coordinator; verifier không gửi ngược về specialist, không gọi lại MCP.
- Trace chỉ chứa decision code và số đếm quan sát được, không chứa suy luận.

## 4. Evidence lifecycle

1. Gateway (`mcp_gateway.py`) validate envelope theo `mcp-evidence-response-v1.schema.json`.
2. Specialist kiểm tra thêm `domain` khớp tool (`EXPECTED_DOMAIN`), sai → `INVALID`, bỏ qua.
3. Hợp lệ → lưu `store[tool_name] = {ref, domain, data}` và emit `tool_result_consumed` (actor = specialist, `tool_name`, `evidence_refs=[ref]`).
4. `rules.decide` chỉ nhận `data` (không thấy ref) và trả về tên tool cần trích dẫn (`cite_tools`, và `cite_tools` theo từng claim).
5. Workflow map tên tool → ref từ store của chính case → `evidence_refs` và `claim_assessments[].evidence_refs`. Mọi ref output vì vậy đều đã xuất hiện trong một `tool_result_consumed`.
6. Verifier loại mọi ref không thuộc store của case. Store bị bỏ khi case kết thúc; không tái sử dụng giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi transport (`TimeoutError`, `OSError`, `httpx2.TransportError`) | Có, tối đa 2 lần, backoff 0.5 s | Coi là thiếu evidence | `handoff` `EVIDENCE_PARTIAL/MISSING`, attr tool=`TIMEOUT` |
| Not found (tool trả `is_error`, ví dụ `get_refund_timeline` ~60% case) | Không (deterministic) | Thiếu evidence, không suy đoán dữ liệu | attr tool=`NOT_FOUND` |
| Envelope sai schema / sai domain | Không | Bỏ kết quả | attr tool=`INVALID` |
| Lỗi client không xác định | Không | Thiếu evidence, không crash run | attr tool=`ERROR` |
| Source conflict (dòng trùng/nhiễu giữa các tool) | Không | Rules chọn nguồn và ghi `data_conflicts` | `policy_decided` |
| Rules ném exception | Không | Output an toàn `insufficient_evidence` / `needs_investigation` | `policy_decided` attr `rules_ok=false` |
| Invalid specialist/decision result (sai shape, sai schema) | Không | Verifier sửa nếu an toàn, nếu không → output an toàn | `verification_completed` `FIXED` / `DOWNGRADED` |

## 6. Verification invariants

Verifier (`verify` trong `workflow.py`) kiểm tra trước finalize, sửa deterministic khi an toàn:

- `case_id` bằng case input.
- `evidence_refs` (top-level và từng claim) ⊆ ref trong store của case; loại trùng. Nếu rỗng mà có `get_order` → thêm ref order.
- Entity ids: chuỗi không rỗng, unique, ≤ 20.
- Responsible party `seller` phải là seller trong `seller_ids` (thay bằng seller duy nhất nếu lệch).
- `resolution_actions` unique, ≤ 8.
- `no_action` ⇒ không có refund line, refund = 0, không có action chứa "refund".
- `recommended_refund_brl == round(sum(refund_lines.amount_brl), 2)`.
- `action_required` mà không có action ⇒ hạ xuống `needs_investigation`.
- Confidence (assessment và claim) clamp về [0, 1].
- JSON Schema `l3a-output-v2`; fail ⇒ thay bằng output an toàn và validate lại.

Kết quả: `verification_completed` với `decision_code` `PASS | FIXED | DOWNGRADED`, attributes `checks_run`, `fixes_applied`, `downgraded`, `primary_issue`.

### Luật nghiệp vụ (`rules.decide`)

Claim topic của khách không được tin. Mỗi case chứa kịch bản thật và một bản sao nhiễu bị dời ngày, nên dữ liệu được neo theo timestamp của chính order (`get_order`):

- Item thật: `shipping_limit_date` nằm trong [approved, estimated]; fallback: gần purchase nhất.
- Payment event thật: trong [approved − 1h, approved + 1 ngày]; bản ghi giống hệt nhau gộp làm một (không phải duplicate charge).
- Refund event thật: trong [purchase, opened_at]. `get_refund_timeline` lỗi = không có refund record.
- Shipment event thật: đúng thời điểm giao cho khách. Mọi dòng bị loại được ghi vào `data_conflicts`.

Thứ tự quyết định: refund failed → refund pending → order canceled → unavailable → giao trễ (carrier nhận sau shipping limit: seller, ngược lại: logistics) → reconciliation mismatch → duplicate charge → split payment đủ tổng → giao đúng hạn: `unsupported_claim` → còn lại: `insufficient_evidence`.

Refund tính từ evidence (tổng capture thật, số tiền refund failed, số tiền mismatch, khoản trùng) và đối chiếu `refund_brl` của policy; lệch thì giữ giá trị evidence và hạ confidence. `case_status`, action, loại responsible party lấy từ policy; `party_id` của seller lấy từ item thật (id trong policy là của case mẫu). Mỗi issue chỉ trích dẫn tập tool tối thiểu hỗ trợ kết luận (`CITE_TOOLS`).

## 7. Reproducibility

- Python 3.11, dependency theo `pyproject.toml` (không thêm dependency). Không dùng LLM, không random seed; rules là hàm thuần deterministic.
- Concurrency: case tuần tự; trong một case tối đa 5 specialist song song trên một MCP session.
- Business rules: xem `src/student_agent/rules.py` (docstring đầu file mô tả interface `Decision`).
- Lệnh:

```bash
source .venv/bin/activate
pytest -q                 # gồm tests/test_workflow_offline.py (fake gateway, không cần mạng)
day09 run && day09 validate
day09 package --output dist/submission.zip
```

- Cấu hình qua `.env` (`MCP_ENDPOINT`, `COMPETITION_TEAM_API_KEY`); không ghi API key vào output/trace.
