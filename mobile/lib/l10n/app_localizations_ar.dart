// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Arabic (`ar`).
class AppLocalizationsAr extends AppLocalizations {
  AppLocalizationsAr([String locale = 'ar']) : super(locale);

  @override
  String get appName => 'منصة التدريب';

  @override
  String get welcomeHeadline => 'تدرّب بوعي.\nكُل بوضوح.';

  @override
  String get welcomeBody => 'مدرّبك وخطتك وتقدّمك، في مكان واحد.';

  @override
  String get signIn => 'تسجيل الدخول';

  @override
  String get createAccount => 'إنشاء حساب';

  @override
  String get continueLabel => 'متابعة';

  @override
  String get email => 'البريد الإلكتروني';

  @override
  String get password => 'كلمة المرور';

  @override
  String get username => 'اسم المستخدم';

  @override
  String get phoneNumber => 'رقم الهاتف';

  @override
  String get forgotPassword => 'نسيت كلمة المرور؟';

  @override
  String get signInTitle => 'أهلاً بعودتك';

  @override
  String get signInSubtitle => 'سجّل دخولك لتكمل من حيث توقفت.';

  @override
  String get registerTitle => 'أنشئ حسابك';

  @override
  String get registerSubtitle => 'بضع تفاصيل وتصبح جاهزاً.';

  @override
  String get iAmClient => 'أتدرّب';

  @override
  String get iAmTrainer => 'أدرّب';

  @override
  String get verifyTitle => 'تحقّق من بريدك';

  @override
  String verifySubtitle(String email) {
    return 'أرسلنا رمزاً من 6 أرقام إلى $email.';
  }

  @override
  String get verifyAction => 'تحقّق';

  @override
  String get resendCode => 'إعادة إرسال الرمز';

  @override
  String resendIn(int seconds) {
    return 'إعادة الإرسال خلال $seconds ث';
  }

  @override
  String get codeSent => 'رمز جديد في طريقه إليك.';

  @override
  String get codeRejected => 'هذا الرمز لم يعمل. تحقّق منه وحاول مجدداً.';

  @override
  String get suspendedTitle => 'هذا الحساب غير متاح';

  @override
  String get suspendedBody => 'تواصل مع الدعم لمعرفة المزيد.';

  @override
  String get sessionExpired => 'انتهت جلستك. سجّل دخولك مجدداً.';

  @override
  String get errorNetwork => 'لا يوجد اتصال. تحقّق من الشبكة وحاول مجدداً.';

  @override
  String get errorTimeout => 'استغرق الأمر وقتاً طويلاً. حاول مجدداً.';

  @override
  String get errorServer => 'حدث خطأ من جهتنا. حاول بعد قليل.';

  @override
  String errorRateLimited(int seconds) {
    return 'محاولات كثيرة. حاول مجدداً خلال $seconds ث.';
  }

  @override
  String get errorUnknown => 'حدث خطأ ما.';

  @override
  String get fieldRequired => 'هذا الحقل مطلوب.';

  @override
  String get emailInvalid => 'أدخل بريداً إلكترونياً صحيحاً.';

  @override
  String get passwordTooShort => 'استخدم 8 أحرف على الأقل.';

  @override
  String get retry => 'إعادة المحاولة';

  @override
  String get cancel => 'إلغاء';

  @override
  String get back => 'رجوع';

  @override
  String get signOut => 'تسجيل الخروج';

  @override
  String get language => 'اللغة';

  @override
  String get english => 'English';

  @override
  String get arabic => 'العربية';

  @override
  String get tabToday => 'اليوم';

  @override
  String get tabTrain => 'تدريب';

  @override
  String get tabEat => 'تغذية';

  @override
  String get tabCommunity => 'المجتمع';

  @override
  String get tabMe => 'أنا';

  @override
  String greetingMorning(String name) {
    return 'صباح الخير، $name';
  }

  @override
  String greetingAfternoon(String name) {
    return 'مساء الخير، $name';
  }

  @override
  String greetingEvening(String name) {
    return 'مساء الخير، $name';
  }

  @override
  String get todayWorkoutTitle => 'حصة اليوم';

  @override
  String get todayMealsTitle => 'وجبات اليوم';

  @override
  String get comingSoon => 'قريباً';

  @override
  String get renewTitle => 'انتهى اشتراكك';

  @override
  String get renewBody => 'جدّد اشتراكك لتحتفظ بخطتك ومدرّبك.';

  @override
  String get renewAction => 'تجديد';

  @override
  String get notInPlanTitle => 'غير متاح في باقتك';

  @override
  String get notInPlanBody => 'هذه الميزة ضمن باقة أعلى.';

  @override
  String get profileTitle => 'الملف الشخصي';

  @override
  String get appearance => 'المظهر';

  @override
  String get appearanceSystem => 'حسب النظام';

  @override
  String get appearanceLight => 'فاتح';

  @override
  String get appearanceDark => 'داكن';
}
