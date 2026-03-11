import os
import sys

print(f"Python Version: {sys.version}")
print(f"Working Directory: {os.getcwd()}")
print("-" * 20)
print("Environment Variables Check:")
variables = ["RENDER_EXTERNAL_URL", "PORT", "GOOGLE_REDIRECT_URI", "INSTANCE_ID"]
for var in variables:
    val = os.environ.get(var)
    print(f"{var}: {'[SET]' if val else '[NOT SET]'} -> {val}")
print("-" * 20)
