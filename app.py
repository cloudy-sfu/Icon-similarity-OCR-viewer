import shutil
import socket
import pandas as pd
import paramiko
import yaml
from flask import Flask, render_template, request, send_file, redirect
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sshtunnel import SSHTunnelForwarder
import atexit
import os
from collections import defaultdict
import json
import webbrowser

app = Flask(__name__)

print("Open external services:")
dataset_config_path = "data/cn_trademarks_config.yaml"
with open(dataset_config_path, 'r') as f:
    config = yaml.safe_load(f)
print("[1/6] Start building SSH tunnel.")
server = SSHTunnelForwarder(
    (config['ssh_host'], config['ssh_port']),
    ssh_private_key=config['ssh_private_key_path'],
    ssh_username=config['ssh_username'],
    remote_bind_address=(config['remote_host'], config['remote_port']),
    local_bind_address=('0.0.0.0', config['db_port'])
)
server.start()
print("[2/6] SSH tunnel connected.")

print("[3/6] Start retrieving case indices from the database.")
engine = create_engine(
    f"postgresql+psycopg2://{config['db_username']}:{config['db_password']}@"
    f"{config['db_host']}:{config['db_port']}/{config['db_name']}"
)
cases = pd.read_sql("select case_idx from cases", engine)
n_cases = cases.shape[0]
cases = cases['case_idx'].tolist()
print("[4/6] Case indices retrieved.")

print("[5/6] Start connecting to SFTP service.")
ssh_client = paramiko.SSHClient()
ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
# load private key
private_key = paramiko.RSAKey.from_private_key_file(config['ssh_private_key_path'])
ssh_client.connect(config['ssh_host'], config['ssh_port'], config['ssh_username'],
                   pkey=private_key)  # connect to server
sftp = ssh_client.open_sftp()  # connect to SFTP
print("[6/6] SFTP service connected.")

with open("sql/get_images_per_case.sql", "r") as f:
    query_get_images = f.read()
images_dir = 'tmp/images'
os.makedirs(images_dir, exist_ok=True)
with open("sql/get_boxes_per_case.sql", "r") as f:
    query_get_boxes = f.read()
languages = {0: 'Chinese', 1: 'English'}


def last_value_below_threshold(list_, p):
    # iterate through the list in reverse order and return the first element that is
    # less than p
    # assume list is sorted in ascending order
    for x in reversed(list_):
        if x < p:
            return x
    return list_[0]


def convert_list_s2d(list_0):
    # convert a list of numbers as strings to integers silently
    # if failing to convert an element, drop
    list_1 = []
    for value_0 in list_0:
        try:
            value_1 = int(value_0)
        except ValueError:
            continue
        list_1.append(value_1)
    return list_1


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        case_idx = cases[0]
    else:
        action = request.form.get('action')
        current_case_idx = request.form.get('case_idx', type=int)
        try:  # check if the current case index is in the list
            p = cases.index(current_case_idx)
        except ValueError:
            case_idx = last_value_below_threshold(cases, current_case_idx)
        else:  # if no exception is raised in the try block
            match action:
                case 'goto':
                    case_idx = current_case_idx
                case 'prev':
                    case_idx = cases[p - 1]
                case 'next':
                    case_idx = cases[(p + 1) % n_cases]
                case _:
                    case_idx = current_case_idx
    n_images = download_images(case_idx)
    ocr_languages = convert_list_s2d(request.form.getlist('ocr_languages'))
    boxes = get_boxes(case_idx, ocr_languages)
    boxes = json.dumps(boxes, ensure_ascii=False)
    return render_template(
        'main.html', case_idx=case_idx, languages=languages,
        checked_languages=ocr_languages, boxes=boxes, n_images = n_images
    )


def download_images(case_idx):
    global sftp, server
    images = pd.DataFrame()
    for _ in range(5):  # number of retries
        try:
            images = pd.read_sql_query(query_get_images, engine, params=[(case_idx,)],
                                       index_col='image_idx')
            break
        except OperationalError:
            server = SSHTunnelForwarder(
                (config['ssh_host'], config['ssh_port']),
                ssh_private_key=config['ssh_private_key_path'],
                ssh_username=config['ssh_username'],
                remote_bind_address=(config['remote_host'], config['remote_port']),
                local_bind_address=('0.0.0.0', config['db_port'])
            )
            server.start()
    for idx, row in images.iterrows():
        remote_file_path = row['path']
        local_file_path = os.path.join(images_dir, f"tm_{case_idx}_{idx}.png")
        if not os.path.exists(local_file_path):
            for _ in range(5):  # number of retries
                try:
                    sftp.get(remote_file_path, local_file_path)
                    break
                except paramiko.ssh_exception.SSHException:  # server connection dropped
                    ssh_client.connect(config['ssh_host'], config['ssh_port'],
                                       config['ssh_username'],
                                       pkey=private_key)  # connect to server
                    sftp = ssh_client.open_sftp()
    n_images = images.shape[0]
    return n_images


def get_boxes(case_idx, languages_list):
    if languages_list:
        languages_str = tuple(languages_list)
    else:
        languages_str = (-1, )  # assume language codes are all non-negative
    boxes_df = pd.read_sql_query(query_get_boxes, engine,
                                 params=[(case_idx, languages_str)])
    boxes_dict = defaultdict(dict)
    for image_idx, image_rows in boxes_df.groupby('image_idx'):
        for box_idx, box_rows in image_rows.groupby('box_idx'):
            # normally box_rows.shape[0] == 1
            # if not, get the latest inputted value in the database
            box_row_series = box_rows.iloc[-1, :]
            box_row_series.drop(['image_idx', 'box_idx'], inplace=True)
            box_row_dict = box_row_series.to_dict()
            boxes_dict[image_idx][box_idx] = box_row_dict
    return boxes_dict


@app.route('/trademark', methods=['GET'])
def view_image():
    case_idx = request.args.get('case_idx')
    image_idx = request.args.get('image_idx')
    filepath = f'tmp/images/tm_{case_idx}_{image_idx}.png'
    if os.path.isfile(filepath):
        return send_file(filepath)
    else:
        return f'Image "tm_{case_idx}_{image_idx}.png" does not exist.'


@app.route('/clear_cache')
def clear_cache():
    shutil.rmtree(images_dir, ignore_errors=True)
    os.makedirs(images_dir)
    return redirect('/')


def find_available_port_skip2(start_port):
    # special: skip db_port and ssh_port
    tries = 100  # try times
    for i in range(tries):
        this_port = start_port + i
        if (this_port == config['db_port']) or (this_port == config['ssh_port']):
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", this_port))
            s.close()
            return this_port
        except OSError:
            pass
    raise Exception(f"Tried {tries} times, no available port from {start_port} to "
                    f"{start_port + tries}.")


def on_exit():
    print("Close external services:")
    global server, sftp, ssh_client
    server.close()
    print("[1/2] SSH tunnel closed.")
    sftp.close()
    ssh_client.close()
    print("[2/2] SFTP service closed.")


atexit.register(on_exit)

if __name__ == '__main__':
    port = find_available_port_skip2(5000)
    webbrowser.open_new_tab(f'http://localhost:{port}')
    app.run(port=port)
