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

from server import FileHandler

BASE_URL = f"http://localhost:{TEST_PORT}"

def run_server():
    # Allow address reuse to avoid "Address already in use" errors
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", TEST_PORT), FileHandler) as httpd:
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

def test_upload_and_rolling_deletion():
    print("Starting upload test...")
    # Upload 105 files
    for i in range(105):
        filename = f"file_{i}.txt"
        content = f"content {i}"
        
        # Put request
        resp = requests.put(f"{BASE_URL}/{filename}", data=content.encode())
        if resp.status_code != 201:
            print(f"Failed to upload {filename}: {resp.status_code} {resp.text}")
        assert resp.status_code == 201
        
        # Small sleep to ensure timestamp diff for sorting
        time.sleep(0.02)
        
    # Check total files
    files = [f for f in os.listdir(TEST_DIR) if not f.startswith('.')]
    print(f"Total files remaining: {len(files)}")
    assert len(files) == 100
    
    # Check expected files
    # We uploaded 0 to 104 (105 files).
    # 5 files should be deleted. 0, 1, 2, 3, 4 should be gone.
    # 5 to 104 should be there.
    
    assert not os.path.exists(os.path.join(TEST_DIR, "file_0.txt")), "file_0 should be deleted"
    assert not os.path.exists(os.path.join(TEST_DIR, "file_4.txt")), "file_4 should be deleted"
    assert os.path.exists(os.path.join(TEST_DIR, "file_5.txt")), "file_5 should exist"
    assert os.path.exists(os.path.join(TEST_DIR, "file_104.txt")), "file_104 should exist"

def test_homepage_list():
    print("Testing homepage...")
    resp = requests.get(BASE_URL)
    assert resp.status_code == 200
    html = resp.text
    
    # Check if latest file is in HTML
    assert "file_104.txt" in html
    # Check if deleted file is NOT in HTML
    assert "file_0.txt" not in html
    assert "file_4.txt" not in html
    assert "file_5.txt" in html
