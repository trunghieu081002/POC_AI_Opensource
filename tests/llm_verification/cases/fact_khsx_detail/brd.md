# Bảng sự kiện: chi tiết kế hoạch sản xuất (fact_khsx_detail)

HG cần một bảng sự kiện ở tầng silver, mỗi dòng là **một dòng chi tiết kế hoạch sản xuất (KHSX)**,
để các báo cáo sau ghép với kế hoạch, người quản lý sản xuất và người phụ trách A&R.
Nguồn: bảng `x_music_plan_detail` (xem schema nguồn đã xác minh).

## Phạm vi dòng

1. Mỗi dòng của bảng nguồn cho **đúng một** dòng đầu ra. Không lọc theo trạng thái (dòng nháp,
   đã duyệt, đã huỷ… đều giữ), không gộp, không bỏ dòng nào.
2. `id` là khoá chính của nguồn: không NULL, không trùng.

## Đầu ra

Bảng `fact_khsx_detail` trong schema `silver`, nạp lại toàn bộ mỗi lần chạy (full snapshot).

| Cột | Kiểu | Lấy từ | Quy tắc |
|---|---|---|---|
| `fact_khsx_detail_sk` | text | `id` | khoá thay thế (surrogate key) theo chuẩn kho HG: macro `generate_surrogate_key` của gói `dbt_utils` (đã có sẵn trong template), áp dụng trên **đúng một cột** `id` |
| `production_plan_detail_id` | text | `id` | mã chi tiết KHSX |
| `production_plan_id` | text | `plan_id` | mã kế hoạch |
| `production_manager` | text | `produce_manager` | mã người quản lý sản xuất |
| `ar_manager` | text | `ar_id` | mã người phụ trách A&R |
| `status` | text | `state` | trạng thái của dòng chi tiết |

Quy tắc chung cho **mọi cột trừ khoá thay thế**: giá trị được chuyển sang text, cắt khoảng trắng
đầu/cuối; giá trị NULL, rỗng hoặc chỉ gồm khoảng trắng thành **NULL**. Giá trị NULL ở các cột nguồn
không làm pipeline lỗi và dòng đó vẫn được giữ.

Không đưa vào đầu ra bất kỳ cột nguồn nào khác (ví dụ `quality`, `original_song_name`).

## Kiểm tra chất lượng (bắt buộc)

- dbt test trên `fact_khsx_detail.fact_khsx_detail_sk`: **`unique`** và **`not_null`**, cả hai với
  mức độ (severity) **`error`** — vi phạm phải làm stage lỗi, không chỉ cảnh báo.
- Bảng nguồn đã nạp phải có ít nhất 1 dòng.
- `fact_khsx_detail_sk` và `production_plan_detail_id` không được NULL trong bảng đầu ra.
