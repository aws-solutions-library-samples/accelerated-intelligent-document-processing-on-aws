resource "aws_dynamodb_table" "chat_document_sessions_table" {
  name = "${substr("${var.stack_name}-ChatDocumentSessionsTable", 0, 246)}-${substr(md5("${var.stack_name}-ChatDocumentSessionsTable"), 0, 8)}"
  hash_key = "sessionId"
  lifecycle {
    ignore_changes = [name]
  }
}
