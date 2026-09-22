import requests
import random
import time
import argparse

API_URL = "http://localhost:8000"

def generate_normal_traffic():
    print(f"Sending normal traffic to {API_URL}/books ... (Press Ctrl+C to stop)")
    while True:
        try:
            requests.get(f"{API_URL}/books")
            book_id = random.choice([1, 2, 3, 4, 5])
            requests.get(f"{API_URL}/books/{book_id}")
            if random.random() < 0.2:
                new_book = {
                    "id": random.randint(1, 100),
                    "title": "Random Title",
                    "author": "Random Author",
                    "price": round(random.uniform(5.0, 25.0), 2)
                }
                requests.post(f"{API_URL}/books", json=new_book)
        except requests.exceptions.RequestException as e:
            print(f"Connection error: {e}")
        time.sleep(random.uniform(0.1, 1.0))

def run_cardinality_experiment():
    print("Running Cardinality Explosion Experiment...")
    for i in range(100):
        try:
            requests.post(f"{API_URL}/experiments/cardinality")
            print(f"Sent request {i+1}/100 to cardinality endpoint")
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
        time.sleep(0.1)
    print("Cardinality experiment finished.")

def toggle_delay():
    try:
        res = requests.post(f"{API_URL}/experiments/toggle-delay")
        print(f"Server response: {res.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['normal', 'cardinality', 'toggle-delay'], default='normal')
    args = parser.parse_args()

    if args.mode == 'normal':
        generate_normal_traffic()
    elif args.mode == 'cardinality':
        run_cardinality_experiment()
    elif args.mode == 'toggle-delay':
        toggle_delay()
