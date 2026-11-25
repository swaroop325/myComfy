#!/bin/bash
# ComfyUI Infrastructure Management Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/terraform"
cd "$TERRAFORM_DIR"

KEY_FILE="comfyui-key.pem"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo -e "${BLUE}"
    echo "=========================================="
    echo "   ComfyUI AWS Infrastructure Manager"
    echo "=========================================="
    echo -e "${NC}"
}

get_instance_ip() {
    terraform output -raw instance_public_ip 2>/dev/null || echo ""
}

menu() {
    print_header
    echo -e "${GREEN}1)${NC} Deploy (terraform apply)"
    echo -e "${GREEN}2)${NC} Destroy (terraform destroy)"
    echo -e "${GREEN}3)${NC} SSH into instance"
    echo -e "${GREEN}4)${NC} View user_data logs"
    echo -e "${GREEN}5)${NC} Check ComfyUI service status"
    echo -e "${GREEN}6)${NC} View ComfyUI logs (live)"
    echo -e "${GREEN}7)${NC} Check GPU status"
    echo -e "${GREEN}8)${NC} Show terraform outputs"
    echo -e "${GREEN}0)${NC} Exit"
    echo ""
    echo -n "Select option: "
}

deploy() {
    echo -e "${YELLOW}Initializing Terraform...${NC}"
    terraform init

    echo -e "${YELLOW}Planning deployment...${NC}"
    terraform plan

    echo ""
    read -p "Proceed with deployment? (y/n): " confirm
    if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
        echo -e "${GREEN}Deploying infrastructure...${NC}"
        terraform apply -auto-approve

        echo ""
        echo -e "${GREEN}Deployment complete!${NC}"
        echo ""
        terraform output

        echo ""
        echo -e "${YELLOW}Note: Server will reboot after bootstrap (~5 min).${NC}"
        echo -e "${YELLOW}Wait for reboot, then run option 4 to check logs.${NC}"
    else
        echo "Deployment cancelled."
    fi
}

destroy() {
    echo -e "${RED}WARNING: This will destroy all infrastructure!${NC}"
    read -p "Are you sure? Type 'destroy' to confirm: " confirm
    if [[ "$confirm" == "destroy" ]]; then
        echo -e "${YELLOW}Destroying infrastructure...${NC}"
        terraform destroy -auto-approve
        echo -e "${GREEN}Infrastructure destroyed.${NC}"
    else
        echo "Destroy cancelled."
    fi
}

ssh_connect() {
    IP=$(get_instance_ip)
    if [[ -z "$IP" ]]; then
        echo -e "${RED}No instance found. Deploy first.${NC}"
        return
    fi

    if [[ ! -f "$KEY_FILE" ]]; then
        echo -e "${RED}SSH key not found: $KEY_FILE${NC}"
        return
    fi

    echo -e "${GREEN}Connecting to $IP...${NC}"
    ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no ubuntu@"$IP"
}

view_user_data_logs() {
    IP=$(get_instance_ip)
    if [[ -z "$IP" ]]; then
        echo -e "${RED}No instance found. Deploy first.${NC}"
        return
    fi

    echo -e "${GREEN}Fetching user_data logs from $IP...${NC}"
    ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no ubuntu@"$IP" \
        "sudo cat /var/log/user-data.log 2>/dev/null || echo 'Log not found yet - instance may still be bootstrapping'"
}

check_service_status() {
    IP=$(get_instance_ip)
    if [[ -z "$IP" ]]; then
        echo -e "${RED}No instance found. Deploy first.${NC}"
        return
    fi

    echo -e "${GREEN}Checking ComfyUI service status...${NC}"
    ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no ubuntu@"$IP" \
        "sudo systemctl status comfyui --no-pager || echo 'Service not yet available'"
}

view_comfyui_logs() {
    IP=$(get_instance_ip)
    if [[ -z "$IP" ]]; then
        echo -e "${RED}No instance found. Deploy first.${NC}"
        return
    fi

    echo -e "${GREEN}Streaming ComfyUI logs (Ctrl+C to stop)...${NC}"
    ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no ubuntu@"$IP" \
        "sudo journalctl -u comfyui -f"
}

check_gpu_status() {
    IP=$(get_instance_ip)
    if [[ -z "$IP" ]]; then
        echo -e "${RED}No instance found. Deploy first.${NC}"
        return
    fi

    echo -e "${GREEN}Checking GPU status...${NC}"
    ssh -i "$KEY_FILE" -o StrictHostKeyChecking=no ubuntu@"$IP" \
        "nvidia-smi 2>/dev/null || echo 'NVIDIA drivers not yet loaded - wait for reboot'"
}

show_outputs() {
    echo -e "${GREEN}Terraform Outputs:${NC}"
    terraform output
}

# Main loop
while true; do
    menu
    read choice
    echo ""

    case $choice in
        1) deploy ;;
        2) destroy ;;
        3) ssh_connect ;;
        4) view_user_data_logs ;;
        5) check_service_status ;;
        6) view_comfyui_logs ;;
        7) check_gpu_status ;;
        8) show_outputs ;;
        0) echo "Goodbye!"; exit 0 ;;
        *) echo -e "${RED}Invalid option${NC}" ;;
    esac

    echo ""
    read -p "Press Enter to continue..."
    clear
done
