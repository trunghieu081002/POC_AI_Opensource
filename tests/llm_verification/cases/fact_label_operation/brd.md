# Bảng sự kiện: vận hành phát hành (fact_label_operation)

HG cần một bảng ở tầng silver, mỗi dòng là **một bản ghi âm (bài hát) đã có mã ISRC**, kèm trạng thái
phát hành, ngày phát hành, nhà phân phối và đường dẫn chứng nhận nghiệm thu của đơn mua tương ứng.
Nguồn: `x_music_song` (bài hát) và `x_acceptance_cert` (chứng nhận nghiệm thu) — xem schema nguồn đã xác minh.

## Các bước, theo đúng thứ tự

1. **Lọc bài hát.** Chỉ giữ bài hát có `active` là đúng (true). `active` NULL hoặc false đều bị loại.
   Đồng thời chỉ giữ bài hát có ISRC: sau khi cắt khoảng trắng đầu/cuối, `isrc` phải khác NULL và khác rỗng
   (ISRC NULL, rỗng, hoặc chỉ gồm khoảng trắng đều bị loại).
2. **Ghép chứng nhận.** Với mỗi bài hát còn lại, tìm chứng nhận có cùng `purchase_order_id` (so sánh như số
   nguyên). Bài hát **không có** chứng nhận nào — kể cả khi `purchase_order_id` của nó là NULL — **vẫn được giữ**,
   với đường dẫn chứng nhận để trống (NULL).
3. **Loại trùng theo ISRC.** Trong các bài hát còn lại sau bước 1, mỗi giá trị ISRC chỉ cho **một** dòng đầu ra:
   dòng của bài hát có **`id` nhỏ nhất**. Nhóm theo giá trị `isrc` **nguyên văn như trong nguồn, chưa cắt khoảng
   trắng**: hai giá trị chỉ khác nhau ở khoảng trắng đầu/cuối (ví dụ `" VN01"` và `"VN01"`) là **hai ISRC khác
   nhau** và cho hai dòng. Việc cắt khoảng trắng chỉ áp dụng cho giá trị **xuất ra** ở bước 4.
   Bài hát bị loại ở bước 1 không tham gia nhóm (một bài inactive có `id` nhỏ hơn không "chiếm chỗ").
4. **Định dạng đầu ra** (bảng dưới).

## Nhiều chứng nhận cho cùng một đơn mua — câu hỏi mở (blocker B1)

HG **chưa có** quy tắc chọn khi một `purchase_order_id` có từ hai chứng nhận trở lên. Đây là câu hỏi mở với HG,
không phải để pipeline tự quyết: **không được** tự đặt quy tắc chọn (chứng nhận mới nhất, `id` nhỏ nhất, ghép
chuỗi, …). Trong phạm vi bài này, mỗi `purchase_order_id` có **tối đa một** chứng nhận; pipeline phải **kiểm tra
điều đó và lỗi** nếu dữ liệu vi phạm: một gate `unique` trên cột `purchase_order_id` của bảng `x_acceptance_cert`
ở stage landing. Ngoài gate đó, không xử lý gì khác cho trường hợp này.

## Đầu ra

Bảng `fact_label_operation` trong schema `silver`, nạp lại toàn bộ mỗi lần chạy (full snapshot). Đúng 7 cột dưới
đây, không cột nào khác:

| Cột | Kiểu | Lấy từ | Quy tắc |
|---|---|---|---|
| `fact_label_operation_sk` | text | `x_music_song.id` | khoá thay thế theo chuẩn kho HG: macro `generate_surrogate_key` của gói `dbt_utils` (có sẵn trong template) trên **đúng một cột** `id` của bài hát |
| `resource_id` | text | `x_music_song.id` | |
| `release_status` | text | `x_music_song.release_state` | |
| `release_date` | timestamp (không múi giờ) | `x_music_song.release_date` | ngày phát hành, đổi sang timestamp (00:00:00); NULL giữ NULL |
| `acceptance_url` | text | `x_acceptance_cert.acceptance_link` | NULL nếu không có chứng nhận |
| `isrc` | text | `x_music_song.isrc` | |
| `distributor` | text | `x_music_song.distro` | mã nhà phân phối |

Quy tắc chung cho mọi cột text **trừ khoá thay thế**: giá trị được chuyển sang text, cắt khoảng trắng đầu/cuối;
NULL, rỗng hoặc chỉ gồm khoảng trắng thành **NULL**. Một mã số lưu dạng số (ví dụ `distro` = 12) xuất ra là
`"12"`.

## Kiểm tra chất lượng (bắt buộc)

- dbt test trên `fact_label_operation.fact_label_operation_sk`: **`unique`** và **`not_null`**, severity **`error`**.
- Gate `unique` trên `x_acceptance_cert.purchase_order_id` ở stage landing (blocker B1 ở trên).
- Bảng `x_music_song` đã nạp phải có ít nhất 1 dòng.
