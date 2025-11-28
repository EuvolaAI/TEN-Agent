SERVER := aibum_playground
cur_dateTime := $(shell date +'%m%d%H%M')
image_address_aws := 584754022186.dkr.ecr.us-east-1.amazonaws.com/aibum/${SERVER}:${cur_dateTime}
latest_aws := 584754022186.dkr.ecr.us-east-1.amazonaws.com/aibum/${SERVER}:latest

#递归删除当前目录下所有临时文件
clean:
	rm -rf .next

build: clean
	docker build -t ${image_address} .

# upload: build
# 	docker build -t ${image_address} .
# 	# docker push ${image_address}
# 	# rm ${SERVER}
# 	# docker rmi ${image_address}

upload:
	aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 584754022186.dkr.ecr.us-east-1.amazonaws.com
	docker build --platform linux/amd64 --build-arg DEPLOY_ENV=Production -t ${image_address_aws} -t ${latest_aws} . --push
	docker rmi ${image_address_aws} ${latest_aws} || true

# deploy: upload
# 	kubectl set image deployment/${SERVER} ${SERVER}=${image_address}
# 	kubectl rollout status deployment/${SERVER}