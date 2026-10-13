# B1: gate bắt buộc phải fail, không được quarantine rồi tiếp tục

`required_gates` trước đây chỉ đối chiếu stage/type/table/columns, nên gate B1 đúng cấu trúc nhưng có quarantine 100% vẫn có thể đi tiếp. Contract mới khai báo `on_violation: fail`; harness và A2 cùng từ chối bất kỳ quarantine nào trên stage chứa gate đó. Contract không khai báo trường này giữ hành vi cũ.

- L8: fixture sạch, output và dbt test đạt nhưng quarantine 100% bị policy/A2 từ chối; approval không thay đổi.
- L7: hai chứng nhận cùng đơn làm landing lỗi; kiểm journal không có silver transform bắt đầu, không promote, cleanup sạch.
- Prompt, template và SQL đã sinh giữ nguyên. Chỉ chạy scripted, không mở benchmark mới.
- Runner cho phép output riêng từng lượt, tùy chọn cgroup namespace; image headless tắt getty@tty1 để host Docker Desktop/WSL chạy systemd ổn định.

Evidence nằm trong `docs/evidence/b1-regression/`, gồm cả các lượt thất bại do quyền journal, CRLF và cgroup.


## Validation

- Source cuối: `cbac310741ea82ab11e2c3031222a4c64a1f1322`, tree `e53b41c894995080b4781c43c99119e1f3039b77`; full unit checkout Linux sạch: 1254 passed, 25 skipped, exit 0.
- A3 label: 8/8; A3 artist: 8/8; cả hai exit 0, leaks=0, trên commit `fc18c74` / tree `a037dcf9c88e56575be4b1e2819719ba71fe969d`. Runtime, A3 và nội dung B1 không đổi từ fc18c74 đến cbac310.
- cbac310 chỉ sửa ca fault injection DROP-failure của driver bronze: giữ kết nối psql idle qua stdin đến teardown, xác nhận kết nối bằng pg_stat_activity; thay timer 120 giây có thể tự hết hạn trước teardown trên host chậm.
- Bronze chưa khép: lần đầy đủ trên fc18c74 khớp 30/31, leaks=0; ca cuối lỗi DNS dbt Hub. Một lượt khác lệch DROP-failure được giữ nguyên rồi dừng với exit 130, leaks=0. Hai lượt cbac310 chưa qua bootstrap do DNS/preflight PGDG, chưa chạy ma trận.

**Draft / blocked:** cần full bronze 31/31, exit 0, leaks=0 trên cbac310 trước khi đánh dấu regression #39 hoàn tất. Mọi lượt lỗi đều giữ trong `docs/evidence/b1-regression/README.md`. Chưa deploy môi trường thật; chưa gọi model thật hoặc mở benchmark mới.
