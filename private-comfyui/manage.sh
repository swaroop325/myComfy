#!/bin/bash
# Private ComfyUI manager: plan | apply | status | start | stop | tunnel | logs | download-models | remove-instance
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
REGION=${AWS_REGION:-ap-southeast-1}
iid() { terraform output -raw instance_id; }
case "${1:-}" in
  plan) terraform init -input=false >/dev/null; terraform plan ;;
  apply)
    terraform init -input=false >/dev/null; terraform plan -out=plan.bin
    read -r -p "Apply this plan? (y/n) " c; if [ "$c" = y ]; then terraform apply plan.bin; fi ;;
  status)
    aws ec2 describe-instances --region "$REGION" --instance-ids "$(iid)" --query 'Reservations[0].Instances[0].[State.Name,InstanceType,PublicIpAddress]' --output text
    aws ssm describe-instance-information --region "$REGION" --filters Key=InstanceIds,Values="$(iid)" --query 'InstanceInformationList[0].PingStatus' --output text ;;
  start) aws ec2 start-instances --region "$REGION" --instance-ids "$(iid)" >/dev/null; echo started ;;
  stop)  aws ec2 stop-instances  --region "$REGION" --instance-ids "$(iid)" >/dev/null; echo stopping ;;
  tunnel)
    echo "ComfyUI -> http://localhost:8188 (Ctrl-C to close)"
    aws ssm start-session --region "$REGION" --target "$(iid)" --document-name AWS-StartPortForwardingSession --parameters portNumber=8188,localPortNumber=8188 ;;
  logs) aws ssm start-session --region "$REGION" --target "$(iid)" --document-name AWS-StartInteractiveCommand --parameters command="journalctl -u comfyui -f" ;;
  download-models)
    B64=$(base64 < scripts/download_models.sh | tr -d '\n')
    aws ssm send-command --region "$REGION" --instance-ids "$(iid)" --document-name AWS-RunShellScript \
      --parameters "commands=[\"echo $B64 | base64 -d > /tmp/download_models.sh && sudo -u ubuntu bash /tmp/download_models.sh\"]" \
      --query Command.CommandId --output text ;;
  remove-instance) # the data volume is NEVER targeted
    terraform destroy -target=aws_volume_attachment.data -target=aws_instance.comfyui ;;
  *) echo "usage: $0 plan|apply|status|start|stop|tunnel|logs|download-models|remove-instance"; exit 1 ;;
esac
