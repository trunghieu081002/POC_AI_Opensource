# Báo cáo: Tỷ lệ giao hàng đúng hẹn theo tháng (delivery SLA)

Bộ phận vận hành cần biết mỗi tháng có bao nhiêu đơn bán đã được giao, và bao nhiêu đơn được giao đúng hẹn.
Nguồn: đơn bán `sale_order` và phiếu kho `stock_picking` — xem schema nguồn đã xác minh.

## Định nghĩa

- **Tháng của một đơn** là tháng của `date_order` (thời điểm đặt hàng), dạng `YYYY-MM`. Mọi thời điểm trong nguồn
  đều là giờ UTC, không có múi giờ khác.
- **Đơn hợp lệ**: đơn có `state` là `sale` hoặc `done`. Đơn ở mọi trạng thái khác (`draft`, `sent`, `cancel`, …)
  không được tính vào bất kỳ chỉ số nào, kể cả khi đã có phiếu giao.
- **Lần giao** của một đơn: phiếu kho của đơn đó có `picking_type` là `outgoing` và `state` là `done`.
  Phiếu trả hàng (`incoming`), phiếu nội bộ (`internal`) và phiếu chưa hoàn tất (mọi `state` khác `done`) **không
  phải** lần giao. Phiếu không gắn với đơn nào (`sale_id` NULL) bị bỏ qua.
- **Thời điểm giao** của một đơn là `date_done` **sớm nhất** trong các lần giao của đơn đó (một đơn có thể được
  giao làm nhiều lần; chỉ lần đầu tiên quyết định đúng hẹn hay không).
- **Đơn đã giao**: đơn hợp lệ có ít nhất một lần giao.
- **Hạn cam kết**: `commitment_date` của đơn; nếu NULL thì hạn là `date_order` cộng đúng **3 ngày** (72 giờ).
- **Đơn giao đúng hẹn**: đơn đã giao có thời điểm giao **nhỏ hơn hoặc bằng** hạn cam kết. Đúng bằng hạn vẫn là
  đúng hẹn; trễ dù một giây là trễ hẹn.

## Đầu ra

Bảng `fct_delivery_sla`, **mỗi tháng một dòng**, chỉ các tháng có ít nhất một đơn hợp lệ:

| cột | kiểu | ý nghĩa |
|---|---|---|
| `month` | text | `YYYY-MM` |
| `eligible_orders` | bigint | số đơn hợp lệ của tháng |
| `delivered_orders` | bigint | số đơn đã giao (mỗi đơn đếm một lần, dù giao bao nhiêu lần) |
| `on_time_orders` | bigint | số đơn giao đúng hẹn |
| `on_time_rate` | numeric | `on_time_orders / eligible_orders × 100`, làm tròn 1 chữ số thập phân (0,05 làm tròn lên) |

Đơn được tính vào tháng đặt hàng của nó, dù được giao trong tháng sau.

## Vận hành

- **Lịch chạy:** hằng ngày lúc 02:00 UTC.
- **Dữ liệu nguồn sai thì dừng:** nếu trong một lần nạp, `sale_order.id` hoặc `stock_picking.id` bị lặp, lần chạy
  phải **dừng ở bước nạp** và không cập nhật bảng báo cáo (bảng giữ nguyên kết quả của lần chạy tốt gần nhất).
  Không được tự chọn một trong các dòng trùng.
