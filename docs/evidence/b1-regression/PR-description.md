B1 trước đây chỉ đối chiếu stage/type/table/columns, nên gate đúng cấu trúc có quarantine 100% vẫn có thể loại dòng rồi tiếp tục. Contract mới khai báo `on_violation: fail`; harness và A2 cùng từ chối quarantine trên stage chứa gate bắt buộc. Contract không khai báo trường này giữ hành vi cũ.

- L7: hai chứng nhận cùng đơn làm landing lỗi; journal không có silver chạy, không promote, approval không thay đổi, cleanup sạch.
- L8: fixture sạch, output và dbt tests đạt nhưng quarantine 100% vẫn bị policy/A2 từ chối; approval không thay đổi.
- DROP-failure giữ kết nối psql đến teardown, xác nhận bằng `pg_stat_activity`. Report thật cho thấy DROP bị chặn bởi 1 session; sau đóng holder không còn tài nguyên cần recovery.
- Prompt, template và SQL đã sinh giữ nguyên. Runner hỗ trợ thư mục evidence riêng và tùy chọn cgroup namespace cho Docker Desktop/WSL.

Validation chạy trên cùng checkout Linux sạch: commit `033b16c7c78af90c4f43292a6888e4cb52c55ee5`, tree `1a03ede545210b1924bca84d016c0a101c2419df`. Thứ tự: unit → label → artist → bronze; sau đó lặp unit để xác nhận numeric exit code bị thiếu ở lượt đầu. Log và capture lỗi đầu được giữ nguyên, có ghi chú trong evidence.

- Unit: 1254 passed, 25 skipped, captured exit 0 ở lượt confirmation.
- Label scripted: 8/8, exit 0, leaks=0.
- Artist scripted: 8/8, exit 0, leaks=0; API timeout mô phỏng là ca đúng kỳ vọng.
- Full bronze: 31/31, exit 0, leaks=0, gồm fact-promote-deploy-run.

Evidence đầy đủ trong [docs/evidence/b1-regression](https://github.com/trunghieu081002/POC_AI_Opensource/tree/fix/b1-fail-required-gate/docs/evidence/b1-regression), gồm mọi lượt thất bại trước đây và closure `033b16c/`. Commit closure chỉ thêm evidence/tài liệu; source được kiểm thử giữ nguyên. Chỉ scripted, không mở benchmark mới, không deploy môi trường thật. Regression của #39 đã khép; cần team review, không tự merge.
