# Báo cáo: Tổng hợp nghệ sĩ (artist summary)

HG cần một dòng tổng hợp cho danh sách đối tác được coi là nghệ sĩ.
Nguồn: bảng `res_partner` (cột `id`, `name`).

## Quy tắc "đối tác được tính"

1. Loại các `id` nằm trong danh sách loại trừ thủ công: **2 và 4**.
2. Loại đối tác có `name` là NULL, rỗng, hoặc chỉ gồm khoảng trắng. Các dòng này
   không được tính vào bất kỳ chỉ số nào và **không được làm pipeline lỗi**.
3. Nếu cùng một `id` xuất hiện nhiều lần trong dữ liệu nguồn (trùng khóa), chỉ tính
   `id` đó **một lần**.
4. Tên được chuẩn hoá bằng cách cắt khoảng trắng đầu/cuối trước khi so sánh.
   So sánh **phân biệt hoa/thường** ("Alice" khác "alice").

## Đầu ra

Bảng `artist_summary` nằm trong schema riêng của pipeline, **đúng 1 dòng**, các cột:

| Cột | Ý nghĩa |
|---|---|
| `artists` | số đối tác được tính (đếm theo `id` duy nhất) |
| `distinct_names` | số tên khác nhau (sau khi chuẩn hoá) trong các đối tác được tính |
| `min_id` | `id` nhỏ nhất trong các đối tác được tính |
| `max_id` | `id` lớn nhất trong các đối tác được tính |

## Kiểm tra chất lượng

- Cả bốn cột của `artist_summary` phải khác NULL.
- Bảng nguồn đã nạp phải có ít nhất 1 dòng.

(Luôn có ít nhất một đối tác được tính; nếu không thì pipeline được phép báo lỗi.)
