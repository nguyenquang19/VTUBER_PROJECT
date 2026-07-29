name: token-saver
description: Kích hoạt khi cần tối ưu hóa tối đa chi phí token và bộ nhớ ngữ cảnh trong quá trình code.


Khi kỹ năng này được kích hoạt, Claude bắt buộc phải tuân thủ nghiêm ngặt các quy tắc cắt giảm token sau đây trong mọi phản hồi:

## 1. Phong Cách Phản Hồi Gọn Nhẹ (Minimalist Output)
- Không chào hỏi, không kết bài, không giải thích dài dòng các khái niệm lý thuyết cơ bản.
- Đi thẳng vào giải pháp: Chỉ giải thích ngắn gọn bằng 1-2 câu lý do tại sao đoạn code được thay đổi.
- Không lặp lại đoạn mã cũ: Nếu sửa đổi một hàm trong file lớn, CHỈ hiển thị đoạn mã cần sửa, sử dụng chú thích `// ... giữ nguyên phần còn lại ...` thay vì in ra toàn bộ file.

## 2. Tiết Kiệm Token Mã Nguồn (Code Optimization)
- Viết code đầy đủ, kiểm tra lại code sau khi xuất ra 
- Không viết các dòng comment giải thích code quá chi tiết, chỉ comment cho các logic thực sự phức tạp.

## 3. Quy Trình Làm Việc Ngăn Ngừa Rework
- Trước khi chỉnh sửa code, hãy phân tích nhanh cấu trúc file trong 1 câu để đảm bảo hiểu đúng ngữ cảnh, tránh viết sai gây tốn token chạy lại.
- Nếu cần kiểm tra kết quả, hãy ưu tiên hướng dẫn người dùng chạy các script kiểm thử có sẵn dưới local thay vì bắt tôi tự suy luận kết quả.
- Tự động nhắc nhở người dùng sử dụng lệnh `/compact` khi nhận thấy cuộc hội thoại kéo dài để giải phóng dung lượng ngữ cảnh cũ không cần thiết.