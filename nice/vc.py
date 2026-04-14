import socket
import threading
import os
import json
from colorama import Fore, Style, init

# Initialize colorama
init()

PORT = 12345

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

clear_screen()

def receive_messages(sock):
    while True:
        try:
            message = sock.recv(4096).decode('utf-8')
            if message:
                # Try to parse JSON (image messages might come as JSON)
                try:
                    msg_obj = json.loads(message)
                    if isinstance(msg_obj, dict) and 'filename' in msg_obj and 'url' in msg_obj:
                        # It's an image message
                        print(f"\r{Fore.CYAN}[Image sent: {msg_obj['filename']}]{Style.RESET_ALL}")
                        print(f"{Fore.CYAN}[Image URL: {msg_obj['url']}]{Style.RESET_ALL}\n{Fore.GREEN}> {Style.RESET_ALL}", end="")
                        continue
                except json.JSONDecodeError:
                    # Not JSON, just normal text message
                    pass

                # Normal text message
                print(f"\r{Fore.CYAN}{message}{Style.RESET_ALL}\n{Fore.GREEN}> {Style.RESET_ALL}", end="")
        except:
            print(f"\n{Fore.RED}Disconnected from server.{Style.RESET_ALL}")
            break

def send_messages(sock, name):
    while True:
        message = input(f"{Fore.GREEN}> {Style.RESET_ALL}").strip()
        if not message:
            continue  # Skip sending if blank
        if message.lower() == "/quit":
            sock.send(f"{name} left the chat.".encode('utf-8'))
            sock.close()
            print(f"{Fore.YELLOW}You left the chat.{Style.RESET_ALL}")
            break
        sock.send(f"{name}: {message}".encode('utf-8'))

def start_client():
    print(f"{Fore.MAGENTA}=== Scooby doo ==={Style.RESET_ALL}")
    host = input(f"{Fore.YELLOW}Enter server IP: {Style.RESET_ALL}")
    name = input(f"{Fore.YELLOW}Enter your name: {Style.RESET_ALL}")

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect((host, PORT))
    except Exception as e:
        print(f"{Fore.RED}Could not connect to server: {e}{Style.RESET_ALL}")
        return

    print(f"{Fore.GREEN}Connected to {host}:{PORT} as {name}! Type /quit to exit.{Style.RESET_ALL}\n")
    client.send(f"{name} joined the chat!".encode('utf-8'))

    threading.Thread(target=receive_messages, args=(client,), daemon=True).start()
    send_messages(client, name)

if __name__ == "__main__":
    try:
        start_client()
    except KeyboardInterrupt:
        print("\n\nGoodbye!")
