#!/bin/bash

# Ensure the script is run as root
if [ "$EUID" -ne 0 ]; then 
    echo -e "\e[31mPlease run this script as root (use sudo)\e[0m"
    exit 1
fi

# Define colors for better output readability
RED="\033[31m"
GREEN="\033[32m"
YELLOW="\033[33m"
BLUE="\033[34m"
PURPLE="\033[35m"
WHITE="\033[97m"
BOLD="\033[1m"
NC="\033[0m" # No color

base_dir="$PWD/aws"  # This will be our main directory for downloads and certificates

# Define log levels
ERROR=0
WARNING=1
INFO=2
DEBUG=3

# Determine LOG_LEVEL based on DEBUG variable
if [ "$DEBUG" = "true" ]; then
    LOG_LEVEL=$DEBUG  # LOG_LEVEL=3
else
    LOG_LEVEL=$INFO  # Default to INFO level (2)
fi

# Set log file path to the current directory
LOG_FILE="$PWD/shelly-aws-provision.log"

# Function to create and check log file permissions
check_log_file_permission() {
    # Check if the log directory is writable, else exit
    if [ ! -w "$PWD" ]; then
        echo -e "${RED}Cannot write to directory: $PWD. Check permissions.${NC}"
        exit 1
    fi

    # Try to create the log file, exit if it fails
    touch "$LOG_FILE" 2>/dev/null || { echo -e "${RED}Failed to create log file: $LOG_FILE. Check permissions.${NC}"; exit 1; }
    log_info "Log file set to: $LOG_FILE"
}

# Logging functions with timestamps for log file output
log_error() {
    [ "$LOG_LEVEL" -ge $ERROR ] && {
        echo -e "${RED}[ERROR] $*${NC}"
        echo "$(date +"%Y-%m-%d %H:%M:%S") [ERROR] $*" >> "$LOG_FILE"
    }
}

log_warning() {
    [ "$LOG_LEVEL" -ge $WARNING ] && {
        echo -e "${YELLOW}[WARNING] $*${NC}"
        echo "$(date +"%Y-%m-%d %H:%M:%S") [WARNING] $*" >> "$LOG_FILE"
    }
}

log_info() {
    [ "$LOG_LEVEL" -ge $INFO ] && {
        echo -e "${WHITE}[INFO] $*${NC}"
        echo "$(date +"%Y-%m-%d %H:%M:%S") [INFO] $*" >> "$LOG_FILE"
    }
}

log_success() {
    [ "$LOG_LEVEL" -ge $INFO ] && {
        echo -e "${GREEN}[SUCCESS] $*${NC}"
        echo "$(date +"%Y-%m-%d %H:%M:%S") [SUCCESS] $*" >> "$LOG_FILE"
    }
}

log_debug() {
    [ "$LOG_LEVEL" -ge $DEBUG ] && {
        echo -e "${PURPLE}[DEBUG] $*${NC}"
        echo "$(date +"%Y-%m-%d %H:%M:%S") [DEBUG] $*" >> "$LOG_FILE"
    }
}

# Function to print stage headers (always displayed)
print_stage_header() {
    echo -e "\n${PURPLE}************** $1 **************${NC}"
}

print_phase_header() {
    echo -e "   \n"
    echo -e "   ${PURPLE}${BOLD}                            -----------${NC}"
    echo -e "   ${PURPLE}${BOLD}############################| $1 |############################${NC}"
    echo -e "   ${PURPLE}${BOLD}                            -----------${NC}"

}

            ############# Phase 1: AWS CLI Setup #############

# Check and install prerequisites
check_prerequisites() {
    print_stage_header "Checking for Prerequisites"
    
    log_info "Checking for necessary prerequisites..."

    local packages=("curl" "unzip" "python3" "jq")
    local missing_packages=()
    
    for pkg in "${packages[@]}"; do
        if ! dpkg -l | grep -qw "$pkg"; then
            missing_packages+=("$pkg")
        fi
    done

    if [ ${#missing_packages[@]} -eq 0 ]; then
        log_success "All prerequisites are already installed."
    else
        log_info "The following packages are missing and will be installed: ${missing_packages[*]}"
        
        # Run `apt update` and check for success before attempting install
        if sudo apt update; then
            if sudo apt install -y "${missing_packages[@]}"; then
                log_success "All missing packages installed successfully."
            else
                log_error "Failed to install one or more packages: ${missing_packages[*]}"
                exit 1
            fi
        else
            log_error "Failed to update package list. Cannot proceed with installation."
            exit 1
        fi
    fi
}


# Check if AWS CLI is installed
check_aws_cli_installed() {
    if command -v aws &> /dev/null; then
        return 0
    else
        return 1
    fi
}

# Handle existing downloaded AWS CLI installer and unzip folder
handle_existing_installer() {
    local aws_zip="$base_dir/awscliv2.zip"
    local aws_unzip_dir="$base_dir/aws"  # Temporary unzip directory

    if [ -f "$aws_zip" ]; then
        log_warning "AWS CLI installer already exists (${aws_zip}). Deleting old installer..."
        rm -f "$aws_zip"
        rm -rf "$aws_unzip_dir"
    fi
}

# Prompt for the download and extraction directory
prompt_for_directory() {
    echo -e "\nPlease specify the directory where you want to download and extract the AWS CLI installer."
    echo "Leave empty to use the current directory."
    read -r -p "Directory (default: current directory): " download_dir

    # Default to current directory if none provided
    [ -z "$download_dir" ] && download_dir="$PWD"

    # Set the base directory for AWS CLI installation files and certificate storage
    base_dir="$download_dir/aws"

    # Create the directory if it doesn’t exist and check permissions
    if [ ! -d "$base_dir" ]; then
        log_warning "Directory does not exist. Creating directory: $base_dir"
        mkdir -p "$base_dir" || { log_error "Failed to create directory: $base_dir"; exit 1; }
    fi

    if [ ! -w "$base_dir" ]; then
        log_error "Directory $base_dir is not writable. Please ensure correct permissions or choose a different directory."
        exit 1
    fi

    log_info "Files will be downloaded and extracted to: ${BLUE}$base_dir${NC}"
}

# Download AWS CLI installer with progress and success message
download_aws_cli() {
    print_stage_header "Downloading AWS CLI"
    
    local aws_zip="$base_dir/awscliv2.zip"
    
    log_info "Downloading AWS CLI (this may take a few moments)..."
    # Use progress indicator with curl
    if curl -# -o "$aws_zip" "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"; then
        echo -e "${GREEN}[SUCCESS] AWS CLI downloaded successfully.${NC}"
    else
        log_error "Failed to download AWS CLI package."
        exit 1
    fi
}

# Unzip AWS CLI installer
unzip_aws_cli() {
    print_stage_header "Unzipping AWS CLI"

    log_info "Unzipping AWS CLI package..."

    local aws_zip="$base_dir/awscliv2.zip"

    # Check if the zip file exists
    if [ ! -f "$aws_zip" ]; then
        log_error "AWS CLI package not found at $aws_zip. Ensure the download completed successfully."
        exit 1
    fi

    # Unzip the package
    if unzip -o -q "$aws_zip" -d "$base_dir"; then
        log_success "AWS CLI package unzipped successfully."
    else
        log_error "Failed to unzip AWS CLI package."
        exit 1
    fi
}

# Install or update AWS CLI
install_or_update_aws_cli() {
    print_stage_header "Installing or Updating AWS CLI"

    cd "$base_dir/aws" || exit 1
    if command -v aws &> /dev/null; then
        # Use --update flag for reinstalling or updating
        if ! sudo ./install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update; then
            log_error "Failed to update AWS CLI."
            exit 1
        fi
        log_success "AWS CLI updated successfully."
    else
        # Install AWS CLI for the first time
        if ! sudo ./install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli; then
            log_error "Failed to install AWS CLI."
            exit 1
        fi
        log_success "AWS CLI installed successfully."
    fi
}

# Test the AWS CLI installation
test_aws_cli() {
    print_stage_header "Testing AWS CLI Installation"

    if ! aws_version=$(aws --version 2>&1); then
        log_error "Something went wrong with the AWS CLI installation. Please check the logs."
        exit 1
    fi
    # Add context to the AWS CLI version output
    log_info "AWS CLI version installed: ${GREEN}$aws_version${NC}"
    log_success "AWS CLI is installed and working correctly."
}

# Configure AWS CLI with SSO or standard credentials
configure_aws_cli() {
    print_stage_header "Configuring AWS CLI"
    log_info "Choose an authentication method:"
    echo -e "   ${WHITE}1) SSO (Single Sign-On)"
    echo -e "   2) Short-term credentials (Access Key and Secret Key)${NC}"
    
    read -r -p "Choose an option (1 or 2): " config_option

    case $config_option in
        1)
            log_info "You chose: ${BLUE}${BOLD}SSO${NC}. Running ${BLUE}${BOLD}aws configure sso${NC}..."
            aws configure sso
            echo -e "${GREEN}[SUCCESS] Configured AWS CLI using ${BLUE}${BOLD}SSO${NC}.${NC}"
            ;;
        2)
            log_info "You chose: ${BLUE}${BOLD}Short-term credentials${NC}. Running ${BLUE}${BOLD}aws configure${NC}..."
            aws configure
            echo -e "${GREEN}[SUCCESS] Configured AWS CLI using ${BLUE}${BOLD}Short-term credentials${NC}.${NC}"
            ;;
        *)
            log_error "Invalid option. Please choose 1 or 2."
            configure_aws_cli  # Retry if invalid option
            ;;
    esac
}

# Clean up downloaded files after successful installation with verification
clean_up() {
    print_stage_header "Cleaning Up Installation Files"

    # Define the paths for the AWS CLI installation files
    local aws_zip="$base_dir/awscliv2.zip"
    local aws_unzip_dir="$base_dir/aws"
    local retries=3
    local delay=2

    log_info "Deleting downloaded and unzipped AWS CLI files..."
    
    # Attempt to remove zip file and AWS CLI directory with retries
    for attempt in $(seq 1 $retries); do
        # Remove the AWS CLI installation files only, leaving certificate directories intact
        rm -f "$aws_zip" && rm -rf "$aws_unzip_dir"
        
        # Check if the files are successfully removed
        if [ ! -f "$aws_zip" ] && [ ! -d "$aws_unzip_dir" ]; then
            log_success "Clean-up completed successfully. Only certificate directories remain in ${BLUE}${BOLD}$base_dir${NC}."
            return 0
        else
            log_warning "Clean-up attempt $attempt failed. Retrying in $delay seconds..."
            sleep $delay
        fi
    done

    log_error "Clean-up incomplete. Ensure you have permission to delete files in $base_dir."
    if [ -d "$aws_unzip_dir" ]; then
        echo -e "${YELLOW}[WARNING] AWS CLI directory '$aws_unzip_dir' still exists. Check if it's in use or requires sudo to delete.${NC}"
    fi
    return 1
}

            ############# Phase 2: AWS IoT Core Setup #############

# Create a Thing in AWS IoT Core
create_thing() {
    print_stage_header "Creating an IoT Thing"

    # List existing Thing Types
    existing_types=$(aws iot list-thing-types --query "thingTypes[].thingTypeName" --output text | tr '\t' '\n')

    if [ -z "$existing_types" ]; then
        log_info "No Thing Types found. You will need to create a new one."
        create_new_thing_type
    else
        log_info "Existing Thing Types:"
        readarray -t types_array <<< "$existing_types"
        thing_type_list=""
        for i in "${!types_array[@]}"; do
            thing_type_list+="  $((i+1))) ${types_array[$i]}\n"
        done
        thing_type_list+="  n) Create a new Thing Type"
        echo -e "${WHITE}$thing_type_list${NC}"

        while true; do
            # Prompt the user to select an existing Thing Type or create a new one
            echo -ne "Choose the Thing Type number or press ${BLUE}${BOLD}'n'${NC} to create a new type: "
            read -r type_choice

            if [ "$type_choice" == "n" ]; then
                create_new_thing_type
                break  # Thing Type will be set in create_new_thing_type()
            elif [[ "$type_choice" =~ ^[0-9]+$ ]]; then
                if [ "$type_choice" -ge 1 ] && [ "$type_choice" -le "${#types_array[@]}" ]; then
                    thing_type="${types_array[$((type_choice-1))]}"
                    log_success "Using existing Thing Type: ${BLUE}${BOLD}$thing_type${NC}"
                    break
                else
                    log_error "Invalid selection. Please choose a valid number."
                fi
            else
                log_error "Invalid selection. Please choose a valid number."
            fi
        done
    fi

    # Ensure that thing_type is set
    if [ -z "$thing_type" ]; then
        log_error "Thing Type is not set. Cannot proceed without a Thing Type."
        exit 1
    fi

    # Prompt for the Thing name, ensuring it’s not empty
    while true; do
        read -r -p "Enter the name of the IoT Thing to create: " thing_name
        if [ -n "$thing_name" ]; then
            break
        else
            echo -e "${RED}Error: Thing name cannot be empty. Please enter a valid name.${NC}"
        fi
    done

    # Create the Thing with the Thing Type
    aws iot create-thing --thing-name "$thing_name" --thing-type-name "$thing_type"

    # Log success message
    log_success "${GREEN}Thing created successfully:${NC} ${BLUE}${BOLD}$thing_name${NC}"
}

# Function to create a new Thing Type
create_new_thing_type() {
    read -r -p "Enter the name of the Thing Type to create: " thing_type
    aws iot create-thing-type --thing-type-name "$thing_type"
    log_success "${GREEN}Thing Type created successfully:${NC} ${BLUE}${BOLD}$thing_type${NC}"
    log_info "The new Thing Type ${BLUE}${BOLD}$thing_type${NC}${WHITE} will be automatically used to create the IoT Thing.${NC}"
}

# Check if the policy exists, or create a new one
check_or_create_policy() {
    print_stage_header "Checking or Creating IoT Policy"

    # List all policies
    existing_policies=$(aws iot list-policies --query "policies[].policyName" --output text | tr '\t' '\n')

    if [ -z "$existing_policies" ]; then
        log_info "No policies found. You will need to create a new one."
        create_new_policy
    else
        log_info "Existing Policies:"
        mapfile -t policies_array <<< "$existing_policies"
        policy_list=""
        for i in "${!policies_array[@]}"; do
            policy_list+="  $((i+1))) ${policies_array[$i]}\n"
        done
        policy_list+="  n) Create a new Policy"
        echo -e "${WHITE}$policy_list${NC}"

        # Prompt user to select an existing policy or create a new one
        #read -r -p "Choose the policy number or press \[${BLUE}${BOLD}\]n\[$NC\] to create a new one: " policy_choice
        echo -ne "Choose the policy number or press ${BLUE}${BOLD}'n'${NC} to create a new one: "
        read -r policy_choice

        if [ "$policy_choice" == "n" ]; then
            create_new_policy
        elif [[ "$policy_choice" -ge 1 && "$policy_choice" -le "${#policies_array[@]}" ]]; then
            policy_name="${policies_array[$((policy_choice-1))]}"
            log_success "Using existing policy: ${BLUE}${BOLD}$policy_name${NC}"
        else
            log_error "Invalid selection. Please choose a valid number."
            check_or_create_policy
            return
        fi
    fi
}

# Function to create a new Policy
create_new_policy() {
    read -r -p "Enter a name for the new policy: " policy_name
    log_info "Using default policy document..."

    # Default policy JSON
    policy_document='{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": "iot:*",
          "Resource": "*"
        }
      ]
    }'

    # Create a new policy
    aws iot create-policy --policy-name "$policy_name" --policy-document "$policy_document"
    log_success "${GREEN}Policy created successfully:${NC} ${BLUE}${BOLD}$policy_name${NC}"
    log_info "The new policy ${BLUE}${BOLD}$policy_name${NC}${WHITE} will be automatically attached to the certificate in the next steps.${NC}"
}

# Generate and attach certificates to the Thing
generate_and_attach_certificate() {
    print_stage_header "Generating and Attaching Certificates"

    # Define the certificate directory within the base directory
    cert_dir="$base_dir/${thing_name}-cert"

    # Ensure the base directory exists and is writable
    if [ ! -d "$base_dir" ]; then
        log_info "Base directory does not exist. Creating directory: $base_dir"
        if ! mkdir -p "$base_dir"; then
            log_error "Failed to create directory: $base_dir. Please check your permissions."
            exit 1
        fi
    fi

    if [ ! -w "$base_dir" ]; then
        log_error "Cannot write to directory: $base_dir. Please ensure correct permissions."
        exit 1
    fi

    # Create the specific certificate directory for the Thing's name
    if ! mkdir -p "$cert_dir"; then
        log_error "Failed to create the directory: ${BLUE}${BOLD}$cert_dir${NC}. Please check your permissions."
        exit 1
    fi

    # Generate a new certificate from AWS IoT
    cert_output=$(aws iot create-keys-and-certificate --set-as-active --query "{certificateArn:certificateArn, certificateId:certificateId, certificatePem:certificatePem, keyPair: {PrivateKey: keyPair.PrivateKey, PublicKey: keyPair.PublicKey}}" --output json)
    
    # Extract details from the generated certificate output
    certificate_arn=$(echo "$cert_output" | jq -r '.certificateArn')
    certificate_id=$(echo "$cert_output" | jq -r '.certificateId')
    certificate_pem=$(echo "$cert_output" | jq -r '.certificatePem')
    private_key=$(echo "$cert_output" | jq -r '.keyPair.PrivateKey')

    # Save the certificate and private key to files in the cert directory
    echo "$certificate_pem" > "$cert_dir/deviceCert_$thing_name.pem"
    echo "$private_key" > "$cert_dir/privateKey_$thing_name.pem"
    
    # Download the Amazon Root CA1 certificate and save it in the same directory
    if ! curl -o "$cert_dir/rootCA.pem" https://www.amazontrust.com/repository/AmazonRootCA1.pem; then
        log_error "Failed to download the Root CA certificate."
        exit 1
    fi
    log_success "Certificate and Private Key created successfully, and Root CA downloaded."

    # Print the location where the certificates are saved
    log_info "Certificates have been saved to the following local directory:"
    echo -e "${BLUE}${BOLD}$cert_dir${NC}"

    # Attach the policy to the certificate
    aws iot attach-policy --policy-name "$policy_name" --target "$certificate_arn"
    
    # Attach the certificate to the Thing
    aws iot attach-thing-principal --thing-name "$thing_name" --principal "$certificate_arn"
    log_success "Certificate and policy attached to the Thing successfully."
}

# Retrieve MQTT client information
retrieve_mqtt_info() {
    print_stage_header "Retrieving MQTT Client Information"

    # Get the AWS IoT Core endpoint
    endpoint=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query "endpointAddress" --output text)
    endpoint_url="$endpoint"
    # Client ID for devices can use the Thing name
    client_id="$thing_name"

    # Information to test MQTT
    log_info "Use the following details to connect your MQTT client or AWS IoT MQTT test client:"

    # Print table header
    echo -e "   ${BLUE}--------------------------------------------${NC}"
    echo -e "   ${BLUE}|       MQTT Connection Details            |${NC}"
    echo -e "   ${BLUE}--------------------------------------------${NC}"

    # Print each row of the table
    echo -e "   ${WHITE}AWS IoT Core/MQTT Endpoint:${NC}     ${GREEN}$endpoint_url${NC}"
    echo -e "   ${WHITE}Client ID (Thing Name):${NC}         ${GREEN}$client_id${NC}"
    echo -e "   ${WHITE}Private Key:${NC}                    ${GREEN}privateKey_$thing_name.pem${NC}"
    echo -e "   ${WHITE}Certificate:${NC}                    ${GREEN}deviceCert_$thing_name.pem${NC}"
    echo -e "   ${WHITE}Root CA:${NC}                        ${GREEN}rootCA.pem${NC}"

    # Print table footer
    echo -e "   ${BLUE}--------------------------------------------${NC}"

    log_info "To subscribe to a topic using the AWS IoT Core MQTT test client, use a topic filter like ${BLUE}${BOLD}'#'${NC} (wildcard) or specify a specific topic."
}

            ############# Phase 3: Shelly Device Setup #############

# Provide the IP address of the Shelly device
validate_ip() {
    local ip="$1"
    # Regular expression for validating IPv4 address
    if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        for part in ${ip//./ }; do
            if (( part < 0 || part > 255 )); then
                return 1  # Invalid if any part is out of range
            fi
        done
        return 0  # Valid IP
    else
        return 1  # Invalid format
    fi
}

get_shelly_ip() {
    print_stage_header "Provide Shelly Device IP"
    
    while true; do
        read -r -p "Enter the IP address of the Shelly device: " shelly_ip
        
        # Validate IP format
        if ! validate_ip "$shelly_ip"; then
            log_error "Invalid IP address format: $shelly_ip. Please enter a valid IPv4 address."
            continue
        fi

        # Check if we can get device info
        device_info=$(curl -s "http://$shelly_ip/rpc/Shelly.GetDeviceInfo")
        
        if [ -z "$device_info" ]; then
            log_error "Unable to fetch device info from Shelly. Please check the IP address or reboot the device."
            echo "Please enter the IP address again."
        else
            log_success "Successfully connected to Shelly device at IP: ${BLUE}${BOLD}$shelly_ip${NC}"
            break  # Exit the loop if the device info is fetched successfully
        fi
    done
}

# Function to check the firmware version and update if needed
check_and_update_firmware() {
    print_stage_header "Checking Shelly Firmware"

    # Define the minimum version required
    minimum_version="1.4.2"

    # Get the current firmware version using Shelly.GetDeviceInfo
    device_info=$(curl -s "http://$shelly_ip/rpc/Shelly.GetDeviceInfo")

    if [ -z "$device_info" ]; then
        log_error "Unable to fetch device info from Shelly. Please check the IP address or device status."
        return
    fi

    # Extract the current firmware version
    current_version=$(echo "$device_info" | jq -r '.ver')

    if [ "$current_version" != "null" ] && [ -n "$current_version" ]; then
        # Color-code the firmware version based on its value
        if dpkg --compare-versions "$current_version" ge "$minimum_version"; then
            firmware_color="$GREEN"
            log_info "Current firmware version: ${firmware_color}$current_version${NC}"
            log_success "Firmware is up to date (version $current_version). No update required."
            return
        elif dpkg --compare-versions "$current_version" ge "1.3.0"; then
            firmware_color="$YELLOW"
            log_warning "Current firmware version: ${firmware_color}$current_version${NC}"
            log_warning "Firmware is below the minimum required version ($minimum_version). Updating firmware..."
        else
            firmware_color="$RED"
            log_error "Current firmware version: ${firmware_color}$current_version${NC}"
            log_error "Firmware is too old. Please update manually."
            exit 1
        fi
    else
        log_error "Firmware version not found. Please check the device information."
        return
    fi

    # If the firmware version is less than the required version, check for updates
    log_info "Checking for firmware updates..."

    # Check for firmware update using Shelly.CheckForUpdate
    for attempt in {1..3}; do
        update_info=$(curl -s "http://$shelly_ip/rpc/Shelly.CheckForUpdate")
        stable_version=$(echo "$update_info" | jq -r '.stable.version')

        if [ "$stable_version" != "null" ] && [ -n "$stable_version" ]; then
            log_info "Firmware update available: $stable_version. Starting update..."

            # Trigger firmware update but discard any output
            curl -s "http://$shelly_ip/rpc/Shelly.Update" > /dev/null

            # Wait for the update to complete
            log_info "Waiting for firmware update to complete..."
            sleep 90  # Wait for 90 seconds for the update to be applied
            break
        else
            log_warning "No update found on attempt $attempt. Retrying..."
            sleep 2
        fi
    done

    if [ "$stable_version" == "null" ] || [ -z "$stable_version" ]; then
        log_info "No updates available or device is already running the latest firmware."
    fi
}

# Upload certificates to Shelly Device
upload_certs_to_shelly() {
    print_stage_header "Uploading Certificates to Shelly Device"

    # Define the path to the cert directory based on the consistent base directory
    local cert_dir="$base_dir/${thing_name}-cert"

    # Paths to device certificate, private key, and CA certificate
    local cert_path="$cert_dir/deviceCert_$thing_name.pem"
    local key_path="$cert_dir/privateKey_$thing_name.pem"
    local ca_path="$cert_dir/rootCA.pem"

    # Check each file before uploading
    if [ ! -f "$cert_path" ]; then
        log_error "Device Certificate file not found at $cert_path. Please ensure the file exists."
        exit 1
    fi

    if [ ! -f "$key_path" ]; then
        log_error "Private Key file not found at $key_path. Please ensure the file exists."
        exit 1
    fi

    if [ ! -f "$ca_path" ]; then
        log_error "CA Certificate file not found at $ca_path. Please ensure the file exists."
        exit 1
    fi

    # Function to upload the certificate in one request
    upload_certificate_single() {
        local file_path=$1
        local endpoint=$2

        # Read the entire file content, including newlines
        local cert_data
        cert_data=$(<"$file_path")

        # Construct JSON payload using jq, ensuring proper JSON escaping
        local json_payload
        json_payload=$(jq -n \
            --arg id "1" \
            --arg method "$endpoint" \
            --arg data "$cert_data" \
            '{id: ($id | tonumber), method: $method, params: {data: $data, append: false}}')

        # For debugging: Print the payload
        log_debug "Uploading full certificate for $endpoint:"
        log_debug "$(echo "$json_payload" | jq . || echo "$json_payload")"

        # Send the POST request
        response=$(curl -s -X POST -H "Content-Type: application/json" \
        -d "$json_payload" \
        "http://$shelly_ip/rpc")

        # Check for success
        log_debug "Response: $response"
        if [[ "$response" != *"len"* ]]; then
            log_error "Failed to upload $file_path with response: $response"
            exit 1
        fi

        log_success "${BLUE}${BOLD}$(basename "$file_path")${NC}${GREEN} uploaded successfully!${NC}"
    }

    # Upload CA Certificate using Shelly.PutUserCA
    log_info "Uploading CA Certificate..."
    upload_certificate_single "$ca_path" "Shelly.PutUserCA"

    # Upload Device Certificate using Shelly.PutTLSClientCert
    log_info "Uploading Device Certificate..."
    upload_certificate_single "$cert_path" "Shelly.PutTLSClientCert"

    # Upload Private Key using Shelly.PutTLSClientKey
    log_info "Uploading Private Key..."
    upload_certificate_single "$key_path" "Shelly.PutTLSClientKey"

    log_success "Certificates uploaded successfully to Shelly device."
}

# Configure MQTT settings on Shelly Device
configure_shelly_mqtt() {
    print_stage_header "Configuring MQTT on Shelly Device"

    # Define the MQTT server (endpoint) and client ID (Thing Name)
    server="$endpoint_url"  # Use the endpoint URL
    client_id="$thing_name"  # Use the Thing Name as the client ID

    # Construct the JSON payload for the MQTT configuration using printf
    mqtt_config=$(printf '{
        "id": 1,
        "method": "Mqtt.SetConfig",
        "params": {
            "config": {
                "enable": true,
                "server": "%s",
                "client_id": "%s",
                "user": null,
                "ssl_ca": "ca.pem",
                "topic_prefix": "%s",
                "rpc_ntf": true,
                "status_ntf": false,
                "use_client_cert": true,
                "enable_control": true
            }
        }
    }' "$server" "$client_id" "$thing_name")

    # Display the configuration payload under debug level
    log_debug "MQTT Configuration being sent to Shelly device:"
    log_debug "$(echo "$mqtt_config" | jq . || echo "$mqtt_config")"

    # Send the configuration to the Shelly device
    log_info "Sending MQTT configuration to Shelly device..."
    response=$(curl -s -X POST -H "Content-Type: application/json" -d "$mqtt_config" "http://$shelly_ip/rpc")

    # Display the response under debug level
    log_debug "Response from Shelly device:"
    log_debug "$(echo "$response" | jq . || echo "$response")"

    # Check for errors in the response
    if echo "$response" | grep -q '"error"'; then
        log_error "Failed to apply MQTT configuration. Response: $response"
    else
        log_success "MQTT configuration applied successfully."
    fi
}

# Reboot the Shelly device to apply changes
reboot_shelly_device() {
    print_stage_header "Rebooting Shelly Device"

    log_info "Rebooting Shelly device to apply changes..."
    response=$(curl -s "http://$shelly_ip/rpc/Shelly.Reboot")

    # Print the response under debug level
    log_debug "Response from reboot command: $response"

    log_info "Waiting for the device to come back online..."

    # Sleep initially to give the device some time to reboot
    sleep 5  # Adjust this as needed

    # Check if the device is back online by attempting to get device info
    max_retries=20  # Number of times to retry (20 retries = 100 seconds total if sleep is 1 second)
    retries=0
    while true; do
        # Try to fetch the device info
        response=$(curl -s "http://$shelly_ip/rpc/Shelly.GetDeviceInfo")
        if [ -n "$response" ] && echo "$response" | jq -e '.id' > /dev/null; then
            log_success "Shelly device rebooted and is back online."
            break
        else
            retries=$((retries + 1))
            if [ "$retries" -ge "$max_retries" ]; then
                log_error "Shelly device did not come back online within the expected time."
                exit 1
            fi
            log_debug "Device not online yet. Retrying in 1 second... (Attempt $retries/$max_retries)"
            sleep 1  # Wait for 1 second before retrying
        fi
    done
}

            ############# Full Script Execution #############

# Full script runs all phases
run_full_script() {
    check_log_file_permission
    print_phase_header 'Phase 1'
    check_prerequisites

    # Phase 1: AWS CLI setup
    if check_aws_cli_installed; then
        read -r -p "AWS CLI is already installed. Do you want to proceed with a fresh installation or update? (y/n): " fresh_install
        if [[ "$fresh_install" =~ ^[Yy]$ ]]; then
            prompt_for_directory
            handle_existing_installer
            download_aws_cli
            unzip_aws_cli
            install_or_update_aws_cli
            clean_up
        else
            log_info "Skipping AWS CLI installation... Proceeding to configuration..."
        fi
    else
        # If no AWS CLI is installed, proceed with fresh installation
        prompt_for_directory
        download_aws_cli
        unzip_aws_cli
        install_or_update_aws_cli
        clean_up
    fi

    test_aws_cli
    configure_aws_cli

    # Phase 2: AWS IoT Core Setup
    print_phase_header 'Phase 2'
    create_thing
    check_or_create_policy
    generate_and_attach_certificate
    retrieve_mqtt_info

    # Phase 3: Shelly Device Setup
    print_phase_header 'Phase 3'
    get_shelly_ip
    check_and_update_firmware
    upload_certs_to_shelly
    configure_shelly_mqtt
    reboot_shelly_device
}

            ############# Start Script Execution #############

# Always run the full script
run_full_script

# End of script