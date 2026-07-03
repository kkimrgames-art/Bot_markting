"""
Shared state for communication between different bot components.
Used primarily for OAuth callbacks when running on a single port (like on Render).
"""

# Dictionary to store captured OAuth callback URIs
# Format: { 'session_id' or 'latest': 'full_callback_url' }
oauth_callback_results = {}
