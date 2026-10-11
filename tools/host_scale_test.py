"""Execute the portable, pixel-exact scaler test under ASan/UBSan."""
import sys
from native_tools import SKIP, run_native

def main():
    code, output = run_native("hosttest_scale.sh")
    print(output, end="" if output.endswith("\n") else "\n")
    if code == 0:
        print("RESULT: PASS pixel-exact scaler (17 checks, ASan/UBSan)")
    return code

if __name__ == "__main__":
    sys.exit(main())
