import sys
sys.path[:0] = ["src", "tests"]
from test_big_data import fill
fill(sys.argv[1])
