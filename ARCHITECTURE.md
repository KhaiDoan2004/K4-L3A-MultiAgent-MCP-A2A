# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Mỗi case chạy một lượt tuần tự, không vòng lặp. `cli.py` phát `case_received`, gọi
`solve_case`, validate output theo schema rồi phát `case_finalized`.

```text
inputs/<case_id>.json
        │
        ▼
   Coordinator ──task_assigned──▶ Order/Item ──▶ get_order, get_order_items
        │                              │
        ├──task_assigned──▶ Payment ───┼──▶ get_payment_timeline, get_refund_timeline
        │                              │
        ├──task_assigned──▶ Shipment ──┼──▶ get_shipment_summary
        │                              │
        │                         (handoff, kèm evidence_refs)
        │                              ▼
        └──task_assigned──▶ Policy ──▶ get_policy ──▶ policy_decided
                                       │
                                  (handoff)
                                       ▼
                                   Verifier ──▶ verification_completed
                                       ▼
                        outputs/<case_id>.json + traces/trace.jsonl
```

Không dùng LLM. Toàn bộ quyết định là luật tất định suy ra từ policy công khai
`EC_POLICY_V1` và từ timeline của chính đơn hàng, nên kết quả tái lập được 100%.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| `coordinator` (`workflow.py`) | case JSON | Phát `task_assigned`, gọi lần lượt từng specialist, lắp output nháp | `task_assigned` → specialists |
| `order_item_agent` (`agents/order_item.py`) | `claimed_order_id` | Lấy order row (nguồn quyền lực nhất), dòng item trong phạm vi, seller, tổng tiền đơn | `Finding` → `policy_agent` |
| `payment_agent` (`agents/payment.py`) | order_id + `Timeline` | Capture, reconciliation, vòng đời refund; lọc dòng ngoài phạm vi | `Finding` → `policy_agent` |
| `shipment_agent` (`agents/shipment.py`) | order_id + `Timeline` | Ngày giao, hạn giao cho carrier, xác định bên gây trễ | `Finding` → `policy_agent` |
| `policy_agent` (`agents/policy.py`) | 3 `Finding` + `get_policy` | Chọn `primary_issue`, tra `case_status`/action/refund/party_type từ policy | `policy_decided`, `handoff` → `verifier_agent` |
| `verifier_agent` (`agents/verifier.py`) | output nháp + tập `evidence_ref` của case | Kiểm invariants, ép về giới hạn schema, hạ cấp khi có lỗi | `verification_completed` |

Phân quyền tool được **cưỡng chế bằng code**: mỗi agent chỉ nhận một `ScopedGateway`
với allowlist riêng (`TOOLS` trong từng module) và `ScopedGateway.call` raise
`PermissionError` nếu gọi ngoài allowlist.

| Actor | Tool được phép |
| --- | --- |
| `order_item_agent` | `get_order`, `get_order_items` |
| `payment_agent` | `get_payment_timeline`, `get_refund_timeline` |
| `shipment_agent` | `get_shipment_summary` |
| `policy_agent` | `get_policy` |
| `coordinator`, `verifier_agent` | không gọi tool |

`get_sellers`, `get_product_context`, `get_customer_history`, `get_order_payments`
không được dùng: seller_id đã có trong `get_order_items`, payment rows đã có trong
`get_payment_timeline`, và điểm evidence có phạt domain không liên quan.

## 3. A2A protocol

Các actor là hàm `async` trong cùng tiến trình. Message envelope là dataclass
`Finding(actor, facts, evidence, warnings)` trong `a2a.py`; `evidence` map
`tool_name → evidence_ref` do server cấp, `Finding.evidence_refs` trả về danh sách
đã bỏ trùng và giữ thứ tự.

- **Correlation:** `case_id` được `ScopedGateway` tự chèn vào mọi tool call và mọi
  trace event; agent không tự truyền `case_id`.
- **Điều kiện handoff:** mỗi specialist chạy đúng một lần rồi handoff sang
  `policy_agent`; `policy_agent` handoff sang `verifier_agent`. Thứ tự cố định,
  không nhánh quay lui, nên không thể lặp vô hạn.
- **Timeout:** `connect_gateway` đặt timeout 300s (connect 30s). Mỗi tool call thử
  tối đa 3 lần với backoff 0.5s/1s/2s.
- **Chỉ trace sự kiện quan sát được:** 7 `event_type` của schema. Nội dung suy luận
  không ghi; mã lý do ngắn nằm ở `decision_code`/`attributes`.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate phong bì theo `mcp-evidence-response-v1`.
2. `ScopedGateway` lưu `evidence["evidence_ref"]` theo tool và phát ngay
   `tool_result_consumed` kèm đúng ref đó (liên kết evidence ↔ trace).
3. `Finding` mang ref về coordinator; `handoff` cũng ghi ref để thấy luồng bằng chứng.
4. `policy_agent` khai báo `relevant_tools` cho từng `primary_issue`; output chỉ lấy
   ref của các tool đã thực sự dùng để kết luận.
5. `verifier` loại mọi ref không thuộc tập thu được **trong chính case này**
   (`allowed_refs`), nên không thể có ref bịa, sai định dạng hay lẫn case khác.
6. Không có state nào sống ngoài một lần `solve_case`, trừ session MCP.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / mất kết nối | Có: 3 lần, backoff 0.5/1/2s; sau đó `ResilientGateway` dựng lại session (tối đa 25 lần cả run) | Agent ghi `warnings`, tool đó trả `None` | `handoff` (thiếu ref của tool lỗi) |
| Tool báo lỗi (ví dụ `get_refund_timeline` khi đơn chưa từng refund) | Không — lỗi tất định | Coi như không có refund evidence | `handoff`; `verification_completed` `decision_code=pass` |
| Không tra được order row | Không | `primary_issue=insufficient_evidence`, `confidence=0.25` | `policy_decided` `decision_code=insufficient_evidence` |
| Policy không định nghĩa issue | Không | Hạ về `insufficient_evidence`, không tự bịa resolution | `policy_decided` |
| Xung đột nguồn (dòng ngoài phạm vi) | Không | Chọn nguồn theo timeline của order, ghi vào `data_conflicts` | `verification_completed` |
| Invariant sai (tổng refund lệch, ref ngoài phạm vi) | Không | Verifier sửa về giá trị an toàn | `verification_completed` `decision_code=fail_<lý do>` |
| Exception ngoài dự kiến | Không | `_unresolved_output()` đúng schema, run không dừng | `verification_completed` `decision_code=fail_internal_error` |

Retry có giới hạn và idempotent (tool chỉ đọc). Thiếu evidence **không** bị chuyển
thành dữ liệu phỏng đoán: nhánh nào thiếu thì hạ `confidence` hoặc trả
`insufficient_evidence`.

## 6. Verification invariants

`agents/verifier.py` kiểm tra trước khi finalize:

- **Schema:** clamp mọi mảng về giới hạn (`evidence_refs` ≤ 30, `data_conflicts` ≤ 5,
  `resolution_actions` ≤ 8 và ≤ 80 ký tự, `ranked_causes` ≤ 5, id ≤ 128 ký tự);
  `cause_code` phải khớp `^[A-Z][A-Z0-9_]{2,79}$`; `confidence` kẹp trong [0, 1].
- **Allowed values:** `primary_issue`, `case_status`, `verdict`, `party_type` phải
  thuộc enum, nếu không thì hạ về `insufficient_evidence`/`needs_investigation`.
- **Evidence ownership:** mọi ref (cả trong `claim_assessments`) phải khớp
  `^ev_...$` **và** nằm trong tập ref của case này.
- **Claim linkage:** mỗi `claim_assessment` phải có `claim_id` lấy từ input.
- **Money totals:** `sum(refund_lines.amount_brl) == recommended_refund_brl`
  (sai số 0.005); `insufficient_evidence` thì refund phải bằng 0.
- **Consistency:** `no_action` mà refund > 0 bị đánh dấu lỗi; `resolution_actions`
  không trùng lặp.

## 7. Reproducibility

- Không dùng model ngôn ngữ, không random, không seed: cùng input và cùng dữ liệu
  MCP thì cho cùng output.
- Python 3.12 (venv), phụ thuộc pin trong `pyproject.toml`; `mcp` 2.2.0, `httpx2`.
- 100 case chạy **tuần tự** (vòng `for` trong `cli.py`), mỗi case tối đa 5 tool call
  (`get_order`, `get_order_items`, `get_payment_timeline`, `get_refund_timeline`,
  `get_shipment_summary`) cộng 1 `get_policy`.
- Lệnh chạy: `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package`.
- **Sửa tương thích SDK:** `mcp_gateway.py` đọc cờ lỗi qua `is_error` (tên
  snake_case của `mcp` 2.x) và vẫn nhận `isError` để tương thích ngược. Không sửa
  chỗ nào khác trong starter, không sửa `contracts/`.
- Không ghi API key vào output, trace hay tài liệu.
