// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for English (`en`).
class AppLocalizationsEn extends AppLocalizations {
  AppLocalizationsEn([String locale = 'en']) : super(locale);

  @override
  String get appName => 'Training Platform';

  @override
  String get welcomeHeadline => 'Train with intent.\nEat with clarity.';

  @override
  String get welcomeBody =>
      'Your coach, your plan and your progress, in one place.';

  @override
  String get signIn => 'Sign in';

  @override
  String get createAccount => 'Create account';

  @override
  String get continueLabel => 'Continue';

  @override
  String get email => 'Email';

  @override
  String get password => 'Password';

  @override
  String get username => 'Username';

  @override
  String get phoneNumber => 'Phone number';

  @override
  String get forgotPassword => 'Forgot password?';

  @override
  String get signInTitle => 'Welcome back';

  @override
  String get signInSubtitle => 'Sign in to pick up where you left off.';

  @override
  String get registerTitle => 'Create your account';

  @override
  String get registerSubtitle => 'A few details and you are in.';

  @override
  String get iAmClient => 'I train';

  @override
  String get iAmTrainer => 'I coach';

  @override
  String get verifyTitle => 'Check your inbox';

  @override
  String verifySubtitle(String email) {
    return 'We sent a 6-digit code to $email.';
  }

  @override
  String get verifyAction => 'Verify';

  @override
  String get resendCode => 'Resend code';

  @override
  String resendIn(int seconds) {
    return 'Resend in ${seconds}s';
  }

  @override
  String get codeSent => 'A new code is on its way.';

  @override
  String get codeRejected => 'That code did not work. Check it and try again.';

  @override
  String get suspendedTitle => 'This account is not available';

  @override
  String get suspendedBody => 'Contact support to find out more.';

  @override
  String get sessionExpired => 'Your session ended. Sign in again.';

  @override
  String get errorNetwork => 'No connection. Check your network and try again.';

  @override
  String get errorTimeout => 'That took too long. Try again.';

  @override
  String get errorServer =>
      'Something went wrong on our side. Try again in a moment.';

  @override
  String errorRateLimited(int seconds) {
    return 'Too many attempts. Try again in ${seconds}s.';
  }

  @override
  String get errorUnknown => 'Something went wrong.';

  @override
  String get fieldRequired => 'This field is required.';

  @override
  String get emailInvalid => 'Enter a valid email address.';

  @override
  String get passwordTooShort => 'Use at least 8 characters.';

  @override
  String get retry => 'Retry';

  @override
  String get cancel => 'Cancel';

  @override
  String get back => 'Back';

  @override
  String get signOut => 'Sign out';

  @override
  String get language => 'Language';

  @override
  String get english => 'English';

  @override
  String get arabic => 'العربية';

  @override
  String get tabToday => 'Today';

  @override
  String get tabTrain => 'Train';

  @override
  String get tabEat => 'Eat';

  @override
  String get tabCommunity => 'Community';

  @override
  String get tabMe => 'Me';

  @override
  String greetingMorning(String name) {
    return 'Good morning, $name';
  }

  @override
  String greetingAfternoon(String name) {
    return 'Good afternoon, $name';
  }

  @override
  String greetingEvening(String name) {
    return 'Good evening, $name';
  }

  @override
  String get todayWorkoutTitle => 'Today\'s session';

  @override
  String get todayMealsTitle => 'Today\'s plate';

  @override
  String get comingSoon => 'Coming soon';

  @override
  String get renewTitle => 'Your subscription has ended';

  @override
  String get renewBody => 'Renew to keep your plan and your coach.';

  @override
  String get renewAction => 'Renew';

  @override
  String get notInPlanTitle => 'Not in your plan';

  @override
  String get notInPlanBody => 'This feature is part of a higher plan.';

  @override
  String get profileTitle => 'Profile';

  @override
  String get appearance => 'Appearance';

  @override
  String get appearanceSystem => 'System';

  @override
  String get appearanceLight => 'Light';

  @override
  String get appearanceDark => 'Dark';
}
