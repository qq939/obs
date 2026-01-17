import os
import time
import shutil
import threading
import requests
import pytest
import socketserver
import asyncio
import websockets
import json

# Set env vars BEFORE importing server
TEST_PORT = 8089
TEST_WS_PORT = TEST_PORT
TEST_DIR = "test_obs"
os.environ["PORT"] = str(TEST_PORT)
os.environ["UPLOAD_DIR"] = TEST_DIR
os.environ["MOCK_DB"] = "true"  # Enable mock database for testing

from server import app

BASE_URL = f"http://localhost:{TEST_PORT}"
WS_URL = f"ws://localhost:{TEST_WS_PORT}/ws"

def run_server():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=TEST_PORT)

@pytest.fixture(scope="module", autouse=True)
def setup_teardown():
    # Cleanup before test
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    
    # Start server (FastAPI + Uvicorn)
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

    # Check if notice board exists
    assert 'class="notice-board"' in html
    assert 'id="notice-content"' in html
    assert 'onclick="resetNotice()"' in html  # The 'x' button
    
    # Check if WebSocket logic exists
    assert 'new WebSocket' in html

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
    print(f"Headers: {resp.headers}")
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
    
    # Upload files with different extensions
    requests.put(f"{BASE_URL}/1.json", data="content")
    time.sleep(0.1)
    requests.put(f"{BASE_URL}/2.txt", data="content")
    time.sleep(0.1)
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

@pytest.mark.asyncio
async def test_websocket_sync():
    print("Testing WebSocket sync...")
    
    # Client 1 connects
    async with websockets.connect(WS_URL) as ws1:
        # Initial message
        init_msg = await ws1.recv()
        data = json.loads(init_msg)
        assert data['type'] == 'init'
        
        # Client 1 updates notice
        test_content = "Hello Sync World"
        await ws1.send(json.dumps({"type": "update", "content": test_content}))
        
        # Client 2 connects
        async with websockets.connect(WS_URL) as ws2:
            # Should receive current content
            init_msg = await asyncio.wait_for(ws2.recv(), timeout=5.0)
            data = json.loads(init_msg)
            assert data['type'] == 'init'
            assert data['content'] == test_content
            
            # Client 2 updates notice
            new_content = "Updated by Client 2"
            await ws2.send(json.dumps({"type": "update", "content": new_content}))
            
            # Client 1 should receive update
            update_msg = await asyncio.wait_for(ws1.recv(), timeout=5.0)
            data = json.loads(update_msg)
            assert data['type'] == 'update'
            assert data['content'] == new_content
            
            # Client 2 resets notice
            await ws2.send(json.dumps({"type": "reset"}))
            
            # Client 1 should receive empty update
            reset_msg = await asyncio.wait_for(ws1.recv(), timeout=5.0)
            data = json.loads(reset_msg)
            assert data['type'] == 'update'
            assert data['content'] == ""
            
            # Client 2 should also receive the broadcast (as per our implementation)
            reset_msg_2 = await asyncio.wait_for(ws2.recv(), timeout=5.0)
            data_2 = json.loads(reset_msg_2)
            assert data_2['type'] == 'update'
            assert data_2['content'] == ""
            
    print("WebSocket sync test passed!")
