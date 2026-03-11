
import datetime
import random

def mock_sort_videos(videos, fetch_order):
    """ميميك منطق الترتيب الموجود في run_cycle"""
    if fetch_order == "oldest":
        # تصاعدي (الأقدم أولاً)
        videos.sort(key=lambda x: x.get("upload_date") or "99999999")
    elif fetch_order == "random":
        random.shuffle(videos)
    else:  # newest
        # تنازلي (الأحدث أولاً)
        videos.sort(key=lambda x: x.get("upload_date") or "00000000", reverse=True)
    return videos

def test_sorting():
    print("--- Testing Fetch Order Logic ---")
    
    sample_videos = [
        {"id": "v1", "upload_date": "20230101", "title": "Old Video"},
        {"id": "v2", "upload_date": "20240101", "title": "New Video"},
        {"id": "v3", "upload_date": None, "title": "Unknown Date"},
        {"id": "v4", "upload_date": "20230601", "title": "Mid Video"},
    ]
    
    # Test Newest
    newest = mock_sort_videos(list(sample_videos), "newest")
    print(f"Newest Order: {[v['id'] for v in newest]}")
    assert newest[0]['id'] == "v2" # Newest
    
    # Test Oldest
    oldest = mock_sort_videos(list(sample_videos), "oldest")
    print(f"Oldest Order: {[v['id'] for v in oldest]}")
    assert oldest[0]['id'] == "v1" # Oldest
    
    # Test Random
    random_order = mock_sort_videos(list(sample_videos), "random")
    print(f"Random Order: {[v['id'] for v in random_order]}")
    
    print("✅ Logic Test Passed!")

if __name__ == "__main__":
    test_sorting()
