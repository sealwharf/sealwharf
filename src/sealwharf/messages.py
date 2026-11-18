"""Chat messages in each supported language, chosen per phone number.

The tone is that of a helpful person: short, friendly, and free of technical
detail the reader cannot act on.
"""

LANGUAGES = ("en", "pt-br")
DEFAULT_LANGUAGE = "en"

MESSAGES = {
    "hello": {"en": "Hello", "pt-br": "Olá"},
    "help": {
        "en": "Hello! Send me a PDF or Word document and I will sign it. If more than one person can sign it, "
              "I will ask who should. To check the signatures of a PDF, send the word \"verify\" and then the PDF. "
              "For your safety, never send certificates or passwords here.",
        "pt-br": "Olá! Envie um PDF ou documento do Word e eu assino. Se mais de uma pessoa puder assinar, "
                 "eu pergunto quem deve. Para conferir as assinaturas de um PDF, envie a palavra \"verificar\" e depois o PDF. "
                 "Para a sua segurança, nunca envie certificados ou senhas por aqui.",
    },
    "received": {
        "en": "Got it! Working on your document...",
        "pt-br": "Recebido! Estou processando o seu documento...",
    },
    "fonts_replaced": {
        "en": "Your Word document was converted to PDF. I replaced the fonts I do not have: {fonts}. "
              "For a perfect result, send the document as PDF.",
        "pt-br": "Seu documento Word foi convertido para PDF. Substituí as fontes que não tenho: {fonts}. "
                 "Para um resultado perfeito, envie o documento em PDF.",
    },
    "signed_by": {
        "en": "All set! Signed by: {signed}.",
        "pt-br": "Tudo certo! Assinado por: {signed}.",
    },
    "no_location": {
        "en": "I could not find a clear place to sign for: {names}. Nothing was signed. "
              "Each signer needs their full name next to one empty signature line or field.",
        "pt-br": "Não encontrei um local claro para a assinatura de: {names}. Nada foi assinado. "
                 "Cada signatário precisa do nome completo junto a uma única linha ou campo de assinatura vazio.",
    },
    "rate_limited": {
        "en": "You have sent many documents in the last hour. Please try again a little later.",
        "pt-br": "Você enviou muitos documentos na última hora. Tente novamente daqui a pouco.",
    },
    "processing_failed": {
        "en": "Sorry, I could not process this document, so nothing was signed. Please check the file and send it again.",
        "pt-br": "Desculpe, não consegui processar este documento, então nada foi assinado. Confira o arquivo e envie novamente.",
    },
    "certificate_rejected": {
        "en": "Sorry, a signing certificate is expired, revoked or could not be validated. Nothing was signed.",
        "pt-br": "Desculpe, um certificado está vencido, revogado ou não pôde ser validado. Nada foi assinado.",
    },
    "refused_secret": {
        "en": "For your safety, never send certificates, keys or passwords here. I ignored the file and did not download it. "
              "If it was a real certificate, consider it exposed and replace it.",
        "pt-br": "Para a sua segurança, nunca envie certificados, chaves ou senhas por aqui. Ignorei o arquivo e não o baixei. "
                 "Se era um certificado real, considere-o exposto e substitua-o.",
    },
    # Who should sign, and asking an owner.
    "choose": {
        "en": "This document can be signed by more than one person. Who should sign? Reply with a number within {minutes} minute(s):\n"
              "{options}\n{all} - All of them\n0 - Cancel",
        "pt-br": "Este documento pode ser assinado por mais de uma pessoa. Quem deve assinar? "
                 "Responda com um número em até {minutes} minuto(s):\n{options}\n{all} - Todos\n0 - Cancelar",
    },
    "needs_approval": {"en": "needs approval", "pt-br": "precisa de aprovação"},
    "choose_unplaced": {
        "en": "I could not find a clear place to sign for: {names}.",
        "pt-br": "Não encontrei um local claro para a assinatura de: {names}.",
    },
    "ask_owner": {
        "en": "To sign as {name}, I need the approval of {owner}. Reply within {minutes} minute(s):\n"
              "1 - Send the document to {owner} and ask for approval\n0 - Cancel",
        "pt-br": "Para assinar como {name}, preciso da aprovação de {owner}. Responda em até {minutes} minuto(s):\n"
                 "1 - Enviar o documento para {owner} e pedir a aprovação\n0 - Cancelar",
    },
    "owner_asked": {
        "en": "Done! I sent the document to {owners} for approval. You will receive it signed as soon as it is approved.",
        "pt-br": "Pronto! Enviei o documento para {owners} aprovar. Você o receberá assinado assim que for aprovado.",
    },
    "owner_busy": {
        "en": "{owner} still has another request waiting for an answer. Please send the document again in a few minutes.",
        "pt-br": "{owner} ainda tem outro pedido aguardando resposta. Envie o documento novamente em alguns minutos.",
    },
    "answer_none": {
        "en": "There is nothing waiting for your answer right now. If you were answering a request, it may have expired.",
        "pt-br": "Não há nada aguardando a sua resposta agora. Se você estava respondendo a um pedido, ele pode ter expirado.",
    },
    "answer_invalid": {
        "en": "That is not one of the options. Please reply with one of the numbers I listed.",
        "pt-br": "Essa não é uma das opções. Responda com um dos números que eu listei.",
    },
    "choice_cancelled": {"en": "Cancelled. Nothing was signed.", "pt-br": "Cancelado. Nada foi assinado."},
    "choice_expired": {
        "en": "I did not get your answer in time, so nothing was signed. Send the document again if you still need it.",
        "pt-br": "Não recebi a sua resposta a tempo, então nada foi assinado. Envie o documento novamente se ainda precisar.",
    },
    # The owner's side of an approval.
    "approval_request": {
        "en": "Hello! {requester} asks you to sign the following document with your digital certificate. "
              "Reply 1 to approve. Reply 0, or simply ignore this message, to refuse.",
        "pt-br": "Olá! Recebemos um pedido de {requester} para assinar o documento a seguir com o seu certificado digital. "
                 "Digite 1 para aprovar e 0 ou ignore para reprovar.",
    },
    "approved": {
        "en": "Approved! The document was signed.",
        "pt-br": "Aprovado! O documento foi assinado.",
    },
    "refused": {"en": "Refused. Nothing was signed.", "pt-br": "Reprovado. Nada foi assinado."},
    "approval_failed": {
        "en": "Sorry, I could not sign the document. Nothing was signed.",
        "pt-br": "Desculpe, não consegui assinar o documento. Nada foi assinado.",
    },
    "approval_rejected": {
        "en": "Sorry, your certificate is expired, revoked or could not be validated. Nothing was signed.",
        "pt-br": "Desculpe, o seu certificado está vencido, revogado ou não pôde ser validado. Nada foi assinado.",
    },
    # Progress of a document with several signers.
    "nobody": {"en": "nobody", "pt-br": "ninguém"},
    "progress": {
        "en": "Signed so far: {signed}. Still waiting for the approval of: {waiting}.",
        "pt-br": "Já assinaram: {signed}. Ainda aguardando a aprovação de: {waiting}.",
    },
    "partial": {
        "en": "Signed by: {signed}. NOT signed by: {missing} (not approved in time, refused or failed).",
        "pt-br": "Assinado por: {signed}. NÃO assinado por: {missing} (não aprovou a tempo, reprovou ou falhou).",
    },
    "not_signed": {
        "en": "The document was not signed. Not approved in time, refused or failed: {missing}.",
        "pt-br": "O documento não foi assinado. Não aprovou a tempo, reprovou ou falhou: {missing}.",
    },
    "expiry_warning": {
        "en": "Heads up: the certificate \"{certificate}\" expires on {date} ({days} day(s) left). Renew it to keep signing.",
        "pt-br": "Atenção: o certificado \"{certificate}\" vence em {date} (faltam {days} dia(s)). Renove-o para continuar assinando.",
    },
    # Checking the signatures of a PDF.
    "verify_prompt": {
        "en": "Sure! Send me the PDF within 5 minutes. I will check its signatures without signing it.",
        "pt-br": "Claro! Envie o PDF em até 5 minutos. Vou conferir as assinaturas sem assinar.",
    },
    "verify_only_pdf": {"en": "I can only check PDF files.", "pt-br": "Só consigo conferir arquivos PDF."},
    "verify_none": {"en": "This PDF has no digital signatures.", "pt-br": "Este PDF não tem assinaturas digitais."},
    "verify_found": {"en": "{count} signature(s) found:", "pt-br": "{count} assinatura(s) encontrada(s):"},
    "verify_valid": {"en": "VALID", "pt-br": "VÁLIDA"},
    "verify_invalid": {
        "en": "INVALID or document changed after signing",
        "pt-br": "INVÁLIDA ou documento alterado após a assinatura",
    },
    "verify_signed_at": {"en": "signed {moment}", "pt-br": "assinado em {moment}"},
    "verify_timestamped": {"en": "with timestamp", "pt-br": "com carimbo de tempo"},
    "verify_expired": {
        "en": "certificate was outside its validity period",
        "pt-br": "certificado fora do período de validade",
    },
    "verify_unknown": {"en": "Unknown signer", "pt-br": "Signatário desconhecido"},
    "verify_all_valid": {"en": "All signatures are valid.", "pt-br": "Todas as assinaturas são válidas."},
    "verify_some_invalid": {
        "en": "At least one signature is NOT valid.",
        "pt-br": "Pelo menos uma assinatura NÃO é válida.",
    },
    "verify_scope": {
        "en": "This checks integrity only, not whether a certificate authority is trusted.",
        "pt-br": "Isto confere apenas a integridade, não se a autoridade certificadora é confiável.",
    },
}


def text(language: str, key: str, **values: object) -> str:
    """Return one message in the requested language, falling back to English."""
    variants = MESSAGES[key]
    return variants.get(language, variants[DEFAULT_LANGUAGE]).format(**values)
