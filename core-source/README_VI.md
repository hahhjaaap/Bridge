# Universal Desktop Bridge — lõi Companion cho Windows

**Phiên bản lõi:** `0.7.0-dev.1` (bản xem trước phát triển)

Thư mục này build trình quản lý desktop và EXE độc lập trên Windows. Nó tách biệt với MCP proxy ở thư mục gốc, có manifest tại `../plugin.json`; hai thành phần có phiên bản riêng. ZIP lõi EXE không đóng gói `plugin.json` hoặc `MCP_CONFIGURATION.example.json` của proxy.

## Build

Dùng checkout sạch, CPython 3.11 Windows x64 đáng tin cậy và dependency build đã ghim phiên bản:

```powershell
python -m pip install -r requirements-build.txt
python Prepare-Vendor.py --python-home C:\Python311 --tunnel .\tools\tunnel-client.exe --tunnel-licenses .\tools\tunnel-LICENSES.txt
python Build-Release.py --revision "YOUR_GIT_COMMIT_SHA" --output .\release-output
python Verify-Package.py .\release-output\Universal-Desktop-Bridge-0.7.0-dev.1-Windows.zip
```

Thay đường dẫn và tên file ví dụ bằng giá trị cục bộ đã xác minh. `Prepare-Vendor.py` không tải dependency: cần cung cấp CPython runtime, tunnel client và gói giấy phép từ nguồn đáng tin, sau khi kiểm tra nguồn và digest. Build sẽ dừng trước PyInstaller nếu runtime offline thiếu file, chứa liên kết hoặc sai inventory/checksum.

## Cài đặt và rollback

Giải nén ZIP đã xác minh vào thư mục mới. Chạy `Install.ps1 -VerifyOnly` trước, rồi `Install.ps1 -NoLaunch` để cài đặt có kiểm soát. Mặc định cài dưới Local AppData của người dùng Windows hiện tại. Đọc script trước khi chạy. `Rollback.ps1` khôi phục bản EXE đã cài trước đó.

Installer không tự cấp quyền owner mới và không đóng gói dữ liệu dự án. Trạng thái runtime, quyền truy cập, token và dữ liệu dự án phải ở cục bộ, không commit lên Git.

## Ranh giới an toàn

SHA-256 giúp phát hiện file bị hỏng nhưng không xác thực nhà phát hành. Bản preview chưa ký mã và chưa được chứng nhận production. Bộ đóng gói từ chối runtime thiếu thành phần, file liên kết, đường dẫn ngoài allowlist, dấu hiệu secret phổ biến và ZIP lồng nhau. Trước khi phân phối, cần kiểm tra cài đặt, rollback, sự đồng ý của owner và MCP end-to-end trong môi trường Windows riêng biệt. Việc gửi lệnh UI/chuột không chứng minh ứng dụng đã chấp nhận hành động.
