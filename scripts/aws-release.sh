set -e
set -x

idp-cli publish --source-dir . --bucket-basename aws-ml-blog --prefix artifacts/genai-idp --region us-west-2 --public
idp-cli publish --source-dir . --bucket-basename aws-ml-blog --prefix artifacts/genai-idp --region us-east-1 --public
idp-cli publish --source-dir . --bucket-basename aws-ml-blog --prefix artifacts/genai-idp --region eu-central-1 --public
idp-cli publish --source-dir . --bucket-basename aws-bigdata-blog-replica --prefix artifacts/genai-idp --region ca-central-1 --public

