import os
import time
import shutil
import threading
import requests
import pytest
import socketserver

# Set env vars BEFORE importing server
TEST_PORT = 8089
TEST_DIR = "test_obs"
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR
os.environ["MOCK_DB"] = "true"  # Enable mock database for testing

from server import FileHandler

BASE_URL = f"http://localhost:{TEST_PORT}"

def run_server():
    # Allow address reuse to avoid "Address already in use" errors
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", TEST_PORT), FileHandler) as httpd:
        httpd.serve_forever()

@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    # Cleanup before test
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    
    # Start server
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # Wait for server to start
    time.sleep(2)
    
    yield
    
    # Cleanup after test
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)

def test_upload_no_limit():
    print("Starting upload test (no limit)...")
    # Upload 25 files (previously limit was 20)
    for i in range(25):
        filename = f"file_{i}.txt"
        content = f"content {i}"
        
        # Put request
        resp = requests.put(f"{BASE_URL}/{filename}", data=content.encode())
        if resp.status_code != 201:
            print(f"Failed to upload {filename}: {resp.status_code} {resp.text}")
        assert resp.status_code == 201
        
    # Check total files
    files = [f for f in os.listdir(TEST_DIR) if not f.startswith('.')]
    print(f"Total files remaining: {len(files)}")
    assert len(files) == 25, "Should have 25 files (no limit)"
    
    # Check all files exist (no rolling deletion)
    assert os.path.exists(os.path.join(TEST_DIR, "file_0.txt")), "file_0 should exist"
    assert os.path.exists(os.path.join(TEST_DIR, "file_4.txt")), "file_4 should exist"
    assert os.path.exists(os.path.join(TEST_DIR, "file_24.txt")), "file_24 should exist"

def test_overwrite_existing_file():
    print("Testing overwrite logic...")
    # Ensure we have 25 files from previous test
    files = [f for f in os.listdir(TEST_DIR) if not f.startswith('.')]
    assert len(files) == 25
    
    # Overwrite an EXISTING file (e.g., file_10.txt)
    filename = "file_10.txt"
    content = "new content for file 10"
    resp = requests.put(f"{BASE_URL}/{filename}", data=content.encode())
    assert resp.status_code == 201
    
    # Verify count is still 25
    files = [f for f in os.listdir(TEST_DIR) if not f.startswith('.')]
    assert len(files) == 25
    
    # Verify file content changed
    with open(os.path.join(TEST_DIR, "file_10.txt"), "r") as f:
        assert f.read() == content

def test_delete_file():
    print("Testing delete file...")
    # Create a file to delete
    filename = "to_be_deleted.txt"
    resp = requests.put(f"{BASE_URL}/{filename}", data=b"delete me")
    assert resp.status_code == 201
    assert os.path.exists(os.path.join(TEST_DIR, filename))
    
    # DELETE request
    resp = requests.delete(f"{BASE_URL}/{filename}")
    assert resp.status_code == 200
    assert not os.path.exists(os.path.join(TEST_DIR, filename))
    
    # Try deleting non-existent file
    resp = requests.delete(f"{BASE_URL}/non_existent.txt")
    assert resp.status_code == 404

def test_homepage_list():
    print("Testing homepage...")
    resp = requests.get(BASE_URL)
    assert resp.status_code == 200
    html = resp.text
    
    # Check if files are in HTML
    assert "file_0.txt" in html
    assert "file_24.txt" in html
    assert "file_10.txt" in html
    
    # Check if URL uses obs.dimond.top
    assert "http://obs.dimond.top/file_0.txt" in html
    assert "http://obs.dimond.top/file_24.txt" in html

    # Check if upload form exists
    assert '<form action="/" method="post" enctype="multipart/form-data">' in html
    assert '<input type="file" name="file" required>' in html
    assert '<input type="submit" value="上传">' in html

def test_json_file_download():
    print("Testing json file download...")
    filename = "test.json"
    content = '{"key": "value"}'
    
    # Upload json file
    resp = requests.put(f"{BASE_URL}/{filename}", data=content.encode())
    assert resp.status_code == 201
    
    # Download json file
    resp = requests.get(f"{BASE_URL}/{filename}")
    assert resp.status_code == 200
    assert resp.text == content
    
    # Check Content-Type and Content-Disposition
    # We want it to be downloadable, so it should have Content-Disposition attachment,
    # OR at least application/json content type.
    # The user says "doesn't support download", which implies it might be opening in browser 
    # or failing.
    print(f"Headers: {resp.headers}")
    # We ideally want application/octet-stream or application/json with attachment
    assert "attachment" in resp.headers.get("Content-Disposition", ""), "Should have Content-Disposition attachment"

def test_special_chars_filename():
    print("Testing special chars filename...")
    # Test with Chinese characters and spaces
    filename = "测试 文件.json"
    content = '{"test": "中文"}'
    
    # URL encode filename for request
    from urllib.parse import quote
    encoded_filename = quote(filename)
    
    # Upload
    resp = requests.put(f"{BASE_URL}/{encoded_filename}", data=content.encode('utf-8'))
    assert resp.status_code == 201
    
    # Verify file exists on disk (decoded name)
    assert os.path.exists(os.path.join(TEST_DIR, filename))
    
    # Download
    resp = requests.get(f"{BASE_URL}/{encoded_filename}")
    assert resp.status_code == 200
    # Response content should match
    assert resp.content.decode('utf-8') == content
    
    # Delete
    resp = requests.delete(f"{BASE_URL}/{encoded_filename}")
    assert resp.status_code == 200
    assert not os.path.exists(os.path.join(TEST_DIR, filename))

def test_sort_by_extension():
    print("Testing sort by extension...")
    # Clear directory first (optional, but cleaner if we rely on exact list)
    # The fixture does this per module, but we are running sequentially.
    # Let's just check relative order in the HTML response.
    
    # Upload files with different extensions
    # 1. json (oldest)
    requests.put(f"{BASE_URL}/1.json", data="content")
    time.sleep(0.1)
    # 2. txt
    requests.put(f"{BASE_URL}/2.txt", data="content")
    time.sleep(0.1)
    # 3. csv (newest)
    requests.put(f"{BASE_URL}/3.csv", data="content")
    
    # Default sort (Time DESC): 3.csv, 2.txt, 1.json
    resp = requests.get(BASE_URL)
    html = resp.text
    pos_csv = html.find("3.csv")
    pos_txt = html.find("2.txt")
    pos_json = html.find("1.json")
    
    assert pos_csv < pos_txt < pos_json, "Default sort should be time DESC"
    
    # Sort by extension (ASC): 3.csv, 1.json, 2.txt
    resp = requests.get(f"{BASE_URL}?sort=ext")
    html = resp.text
    pos_csv = html.find("3.csv")
    pos_json = html.find("1.json")
    pos_txt = html.find("2.txt")
    
    assert pos_csv < pos_json < pos_txt, "Sort by ext should be .csv, .json, .txt"

