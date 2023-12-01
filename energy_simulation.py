import time
import logging
from datetime import datetime

from flask import Flask, request, jsonify, render_template, redirect
from prometheus_api_client import PrometheusConnect

app = Flask(__name__)

power_factors_file = "wind_uk_offshore.txt"


storage_capacity_joules = 0 
stored_energy_joules = 0 
wind_farm_capacity_joules = 0

prometheus_url = "http://prometheus-k8s.monitoring.svc:9090"
#prometheus_url = "http://localhost:9090"
prometheus_query = 'sum by (pod_name)(kepler_container_joules_total{container_namespace="%s"})'
prometheus_client = None

# Namespace to monitor
target_namespace = "workload"  # Replace with the actual namespace

# Dictionary to store previous total energy for each pod
previous_total_energy = {}
stop_simulation = False
simulation_thread = None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_power_factors():
    global power_factors_data

    try:
        with open(power_factors_file, 'r') as file:
            power_factors_data = [float(line.strip()) for line in file]

            if len(power_factors_data) == 262968:
                logger.info("Power factors data loaded successfully.")
            else:
                logger.error("Invalid power factors data: It should contain hourly values for each hour from 01/01/1986 to 31/12/2015.")
    except FileNotFoundError:
        logger.error("Power factors file not found.")
    except Exception as e:
        logger.error(f"Error loading power factors data: {e}")

def read_power_factors(start_date):
    global power_factors_data

    if not power_factors_data:
        load_power_factors()

    if not power_factors_data:
        logger.error("Power factors data not available.")
        return [0.5] * 24

    # Calculate the start index based on the provided start date
    start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
    start_index = (start_datetime - datetime(1986, 1, 1)).days * 24

    return power_factors_data[start_index : start_index + 24]

def get_current_energy_utilization(prometheus_client):
    try:
        # Query Prometheus for the current energy utilization in the specified namespace
        result = prometheus_client.custom_query(prometheus_query % target_namespace)

        # Calculate the difference in energy for each pod
        current_total_energy = {}
        for entry in result:
            pod_name = entry['metric']['pod_name']
            current_total_energy[pod_name] = float(entry['value'][1])

        # Calculate the difference in energy since the last measurement
        energy_difference = {}
        for pod_name, current_energy in current_total_energy.items():
            previous_energy = previous_total_energy.get(pod_name, 0.0)
            energy_difference[pod_name] = current_energy - previous_energy
            previous_total_energy[pod_name] = current_energy
        #print(energy_difference)
        return energy_difference

    except Exception as e:
        logger.error(f"Error fetching energy utilization from Prometheus: {e}")
        return {}

def check_wind_down_instruction():
    global stored_energy_joules, storage_capacity_joules
    # Check if stored energy is less than 20% of capacity
    return stored_energy_joules < 0.2 * storage_capacity_joules

@app.route('/ml_model_epochs', methods=['POST'])
def ml_model_epochs():
    global stored_energy

    data = request.json
    if 'epochs' not in data or 'pod_name' not in data:
        logger.error('Invalid request: Missing "epochs" or "pod_name" in the request.')
        return jsonify({'error': 'Invalid request'}), 400
    else:
        epochs = data['epochs']
        energy = previous_total_energy.get(data['pod_name'],0)
        logger.info(f"ML Model in {data['pod_name']} Consumed {energy}J Energy in {epochs} Epochs")

        # Check for wind down instruction
        if check_wind_down_instruction():
            #Give a chance to complete if model is estimated to complete with 10% battery left
            if 'estimated_total_epochs' in data and data['pod_name'] in previous_total_energy:
                if data['estimated_total_epochs'] < data['epochs']:
                    logger.warning("Wind down instruction: Estimated epochs exceeded")
                    return jsonify({'message': 'Wind down instruction'}), 200
                energy_per_epoch = previous_total_energy[data['pod_name']]/data['epochs']
                energy_to_complete = (data['estimated_total_epochs']-data['epochs'])*energy_per_epoch
                if stored_energy_joules - energy_to_complete >= 0.1 * storage_capacity_joules:
                    return jsonify({'message': 'Complete or wind down before estimated_total_epochs'}), 200
            logger.warning("Wind down instruction: Stored energy is below 20% capacity.")
            return jsonify({'message': 'Wind down instruction'}), 200
        return jsonify({'message': 'Success'}), 200

def simulate_energy_availability(prometheus_client, power_factors, update_interval_sec):
    global previous_total_energy, stored_energy_joules, stop_simulation

    iterations_per_hour = int(3600 / update_interval_sec)
    power_factor_index = 0  # Initialize power_factor_index to 0
    previous_total_energy = {}

    get_current_energy_utilization(prometheus_client) #call first time to initialize the previous_total_energy
    time.sleep(update_interval_sec)
    while not stop_simulation:

        for iteration in range(1, iterations_per_hour + 1):
            current_energy_difference = get_current_energy_utilization(prometheus_client)
            pf = power_factors[power_factor_index % len(power_factors)]
            generated_energy_joules = pf * wind_farm_capacity_joules / iterations_per_hour

            # Get the current energy utilization for all pods in the specified namespace
            current_utilization = sum(current_energy_difference.values())

            net_energy_joules = generated_energy_joules - current_utilization
            stored_energy_joules = min(stored_energy_joules + net_energy_joules, storage_capacity_joules)

            if stop_simulation:
                break

            logger.info(f"Hour {power_factor_index}: "
                  f"PF {pf}, "
                  f"Current Utilization = {current_utilization} J, "
                  f"Generated Energy = {generated_energy_joules/3600000} kWh, "
                  f"Net Energy = {net_energy_joules} J, "
                  f"Stored Energy = {stored_energy_joules/3600000} kWh")

            # Update power factor index every hour
            if iteration % iterations_per_hour == 0:
                power_factor_index += 1

            time.sleep(update_interval_sec)

    # Reset the stop_simulation flag for the next simulation
    stop_simulation = False

def is_valid_date(start_date):
    try:
        date_obj = datetime.strptime(start_date, "%Y-%m-%d")
        return datetime(1986, 1, 1) <= date_obj <= datetime(2015, 12, 31)
    except ValueError:
        return False

@app.route('/start_simulation', methods=['POST'])
def start_simulation_route():
    global target_namespace, prometheus_client, simulation_thread, stop_simulation, stored_energy_joules, storage_capacity_joules, wind_farm_capacity_joules

    logger.info('start_sim_req received')
    if request.method == 'POST':
        # Stop the previous simulation thread
        if simulation_thread:
            stop_simulation = True
            if simulation_thread.is_alive():
                simulation_thread.join()

        # Load the power factors data
        load_power_factors()

        # Start a new simulation with the specified namespace and start date
        namespace = request.form['namespace']
        start_date = request.form['start_date']

        # Validate the start date
        if not is_valid_date(start_date):
            logger.error("Invalid start date. Please choose a date between 01/Jan/1986 and 31/Dec/2015.")
            return redirect('/')

        stored_energy_joules = float(request.form['stored_energy_kwh'])*3600000
        storage_capacity_joules = float(request.form['storage_capacity_kwh'])*3600000
        wind_farm_capacity_joules = float(request.form['wind_farm_production_capacity_kwh'])*3600000/24

        power_factors = read_power_factors(start_date)

        target_namespace = namespace
        logger.info(f"Simulation started/restarted for namespace: {target_namespace}, starting from {start_date}")

        # Restart the simulation thread with the new power factors
        simulation_thread = Thread(target=lambda: simulate_energy_availability(prometheus_client, power_factors, 15))
        simulation_thread.start()

    return redirect('/')

@app.route('/')
def home():
    return render_template('index.html')

if __name__ == "__main__":
    # Create a single PrometheusConnect object outside the main loop
    prometheus_client = PrometheusConnect(url=prometheus_url)

    # Run the Flask app and the simulation in separate threads
    from threading import Thread
    api_thread = Thread(target=lambda: app.run(host='0.0.0.0', port=5001))
    #simulation_thread = Thread(target=lambda: simulate_energy_availability(prometheus_client, 15))

    api_thread.start()
    #simulation_thread.start()

    api_thread.join()
    #simulation_thread.join()
