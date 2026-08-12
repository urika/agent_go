/**
 * 企业门户表单校验库 —— 联系表单/订阅表单校验纯函数。
 */

export interface FieldError {
  field: string;
  message: string;
}

/** 邮箱校验：宽松但正确的格式检查 */
export function isValidEmail(email: string): boolean {
  if (!email || email.length > 254) return false;
  // 简化的 RFC 5322 近似：local@domain.tld
  const re = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  return re.test(email);
}

/** 中国大陆手机号校验（11 位，1 开头） */
export function isValidPhone(phone: string): boolean {
  const re = /^1[3-9]\d{9}$/;
  return re.test(phone);
}

/** 必填校验 */
export function required(value: string | null | undefined, label: string): FieldError | null {
  if (!value || !value.trim()) {
    return { field: label, message: `${label}不能为空` };
  }
  return null;
}

/** 长度校验 */
export function lengthBetween(value: string, min: number, max: number, label: string): FieldError | null {
  const len = value?.length ?? 0;
  if (len < min || len > max) {
    return { field: label, message: `${label}长度需在 ${min}-${max} 之间（当前 ${len}）` };
  }
  return null;
}

/** 统一联系方式校验：邮箱或手机号至少填一个且格式正确 */
export function validateContact(
  contact: { email?: string; phone?: string },
): FieldError[] {
  const errors: FieldError[] = [];
  const email = contact.email ?? "";
  const phone = contact.phone ?? "";
  if (!email.trim() && !phone.trim()) {
    errors.push({ field: "contact", message: "邮箱或手机号至少填写一项" });
    return errors;
  }
  if (email.trim() && !isValidEmail(email)) {
    errors.push({ field: "email", message: "邮箱格式不正确" });
  }
  if (phone.trim() && !isValidPhone(phone)) {
    errors.push({ field: "phone", message: "手机号格式不正确" });
  }
  return errors;
}

/** 联系表单整体校验 */
export function validateContactForm(form: {
  name: string;
  contact: { email?: string; phone?: string };
  message: string;
}): FieldError[] {
  const errors: FieldError[] = [];
  const r1 = required(form.name, "姓名");
  if (r1) errors.push(r1);
  const r2 = lengthBetween(form.message, 10, 1000, "留言内容");
  if (r2) errors.push(r2);
  errors.push(...validateContact(form.contact));
  return errors;
}
