# Báo cáo: Tỷ lệ giao hàng đúng hẹn theo tháng

Bộ phận vận hành cần biết mỗi tháng có bao nhiêu đơn bán được giao đúng hẹn.
Nguồn: đơn bán `sale_order` và phiếu kho `stock_picking` — xem schema nguồn đã xác minh.

Một đơn được giao đúng hẹn nếu hàng được giao trước hạn cam kết.

## Đầu ra

Bảng `fct_delivery_sla`, mỗi tháng một dòng: `month` (`YYYY-MM`), số đơn, số đơn đã giao, số đơn giao đúng hẹn,
và tỷ lệ đúng hẹn (%).
